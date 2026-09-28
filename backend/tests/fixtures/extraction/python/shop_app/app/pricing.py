from dataclasses import dataclass


@dataclass
class Price:
    cents: int

    def with_tax(self, rate):
        return Price(round(self.cents * (1 + rate)))


def total(prices: list, basket: "Basket"):
    return sum(p.cents for p in prices)


class Basket:
    def __init__(self):
        self.items = []

    def add(self, price: Price):
        self.items.append(price)
        return price.with_tax(0.2)

    def merge(self, other: "Basket"):
        for price in other.items:
            self.add(price)
        other.clear()

    def clear(self):
        self.items = []
