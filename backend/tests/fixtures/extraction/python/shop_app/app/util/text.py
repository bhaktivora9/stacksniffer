import re
import unicodedata as ud


def slugify(value):
    value = ud.normalize("NFKD", value)
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def truncate(value, limit=20):
    return value if len(value) <= limit else value[:limit]
