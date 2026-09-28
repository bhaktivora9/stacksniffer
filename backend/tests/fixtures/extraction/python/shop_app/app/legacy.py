from app.models import Order


def migrate(rows):
    return [Order(row["customer"]) for row in rows]


def broken(rows
    return rows


def after():
    return migrate([])
