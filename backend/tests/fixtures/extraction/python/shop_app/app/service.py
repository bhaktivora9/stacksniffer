import logging

import requests

try:
    import ujson as json
except ImportError:
    import json

from . import models
from .models import Order as OrderModel
from .util import slugify
from .util.text import truncate

log = logging.getLogger(__name__)


class OrderService:
    def __init__(self, client=None):
        self.client = client or requests.Session()

    def create(self, customer):
        order = OrderModel(customer)
        log.info("created %s", slugify(customer))
        return order

    def fetch_prices(self, url):
        response = self.client.get(url, timeout=5)
        response.raise_for_status()
        return json.loads(response.text)

    def summary(self, order):
        def label(text):
            return truncate(text, 10)
        return label(order.customer) + models.Order.__name__
