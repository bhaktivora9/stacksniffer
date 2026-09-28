import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self):
        self.store = defaultdict(list)

    def put(self, key, value):
        self.store[key].append(value)
        self._evict()

    def _evict(self):
        if len(self.store) > 100:
            self.store.clear()


class LoggingCache(Cache):
    def put(self, key, value):
        logger.info("put %s", key)
        super().put(key, value)
        self.flush()

    def flush(self):
        self._evict()


def build(kind="plain"):
    cls = LoggingCache if kind == "logging" else Cache
    cache = cls()
    cache.put("a", 1)
    return cache


def main():
    cache = build()
    for key in ("a", "b"):
        cache.put(key, len(key))
    print(sorted(cache.store))


if __name__ == "__main__":
    main()
