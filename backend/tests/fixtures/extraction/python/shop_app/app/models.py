from dataclasses import dataclass, field
from datetime import datetime

from app.util.text import slugify


class TimestampMixin:
    def touch(self):
        self.updated_at = datetime.utcnow()


class AuditMixin:
    def audit(self, action):
        return f"{type(self).__name__}:{action}"


@dataclass
class Order(TimestampMixin, AuditMixin):
    customer: str
    items: list = field(default_factory=list)

    @property
    def slug(self):
        return slugify(self.customer)

    def add(self, sku, quantity=1):
        self.items.append((sku, quantity))
        self.touch()
        return self.audit("add")

    def total(self, prices):
        return sum(prices[sku] * qty for sku, qty in self.items)
