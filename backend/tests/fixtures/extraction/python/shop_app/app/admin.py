from app.models import Order, AuditMixin


class AdminOrder(Order):
    def approve(self):
        self.touch()
        return self.audit("approve")


class Reviewer(AuditMixin):
    pass
