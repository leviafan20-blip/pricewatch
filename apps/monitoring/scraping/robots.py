"""Bounded robots rules with longest-match precedence and a process-local TTL cache."""

from collections import OrderedDict
from dataclasses import dataclass
import fnmatch
import math
import re
import threading
import time
from urllib.parse import quote, urlsplit

from .errors import ScrapingError

_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _octets(value):
    value = quote(value, safe="/%*?=&:+!$,;@[]()'~-._")
    return re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda match: chr(int(match[1], 16)) if chr(int(match[1], 16)) in _UNRESERVED else "%" + match[1].upper(),
        value,
    )


@dataclass(frozen=True)
class RobotsRules:
    rules: tuple = ()
    crawl_delay: float = 0
    deny_all: bool = False

    def can_fetch(self, url):
        if self.deny_all:
            return False
        parsed = urlsplit(url)
        path = _octets((parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
        matches = []
        for allow, rule in self.rules:
            anchored = rule.endswith("$")
            rule = rule[:-1] if anchored else rule
            # fnmatch's public implementation avoids exponential regex backtracking.
            pattern = "".join({"?": "[?]", "[": "[[]", "]": "[]]"}.get(c, c) for c in rule)
            if not anchored:
                pattern += "*"
            if fnmatch.fnmatchcase(path, pattern):
                matches.append((len(rule.replace("*", "")), allow))
        return max(matches)[1] if matches else True


def parse_robots(text, user_agent="PriceWatchBot"):
    if len(text.encode("utf-8")) > 512 * 1024:
        raise ScrapingError("robots_unavailable", "Файл robots.txt слишком большой.")
    groups, agents, rules, delay = [], [], [], 0.0
    has_directives = False
    for line in text.lstrip("\ufeff").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if has_directives:
                groups.append((agents, rules, delay))
                agents, rules, delay, has_directives = [], [], 0.0, False
            if value:
                agents.append(value.lower())
        elif agents:
            has_directives = True
            if key in {"allow", "disallow"} and value:
                if len(value) > 2048 or len(rules) >= 2048:
                    raise ScrapingError("robots_unavailable", "Слишком сложные правила robots.txt.")
                if not value.startswith("/"):
                    raise ScrapingError("robots_unavailable", "Некорректные правила robots.txt.")
                rules.append((key == "allow", _octets(value)))
            elif key == "crawl-delay":
                try:
                    parsed_delay = float(value)
                except ValueError:
                    raise ScrapingError("robots_unavailable", "Некорректный Crawl-delay.") from None
                if not math.isfinite(parsed_delay) or not 0 <= parsed_delay <= 86400:
                    raise ScrapingError("robots_unavailable", "Некорректный Crawl-delay.")
                delay = max(delay, parsed_delay)
    if agents:
        groups.append((agents, rules, delay))
    agent = user_agent.lower()
    selected = [(max((len(a) for a in aa if a != "*" and a in agent), default=-1), rr, dd)
                for aa, rr, dd in groups]
    best = max((item[0] for item in selected), default=-1)
    if best >= 0:
        chosen = [(rr, dd) for size, rr, dd in selected if size == best]
    else:
        chosen = [(rr, dd) for aa, rr, dd in groups if "*" in aa]
    return RobotsRules(tuple(rule for rr, _ in chosen for rule in rr), max((dd for _, dd in chosen), default=0))


def clear_cache():
    with _CACHE_LOCK:
        _CACHE.clear()


def get_cached(key):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= time.monotonic():
            del _CACHE[key]
            return None
        _CACHE.move_to_end(key)
        return value


def cache_rules(key, value, ttl):
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + min(ttl, 86400), value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > 256:
            _CACHE.popitem(last=False)
