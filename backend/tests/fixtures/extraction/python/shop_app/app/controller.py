from app.service import OrderService
from app import models

service = OrderService()


def create_order(customer, skus):
    order = service.create(customer)
    for sku in skus:
        order.add(sku)
    return order


def order_total(order: models.Order, prices):
    return order.total(prices)
