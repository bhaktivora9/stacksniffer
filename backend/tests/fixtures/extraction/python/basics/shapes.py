import math
from dataclasses import dataclass
from functools import lru_cache as cache


class Shape:
    def area(self):
        raise NotImplementedError

    def describe(self):
        return f"{self.name()}: {self.area():.2f}"

    def name(self):
        return type(self).__name__


@dataclass(frozen=True)
class Circle(Shape):
    radius: float

    def area(self):
        return math.pi * self.radius ** 2

    @staticmethod
    def unit():
        return Circle(1.0)


class Square(Shape):
    def __init__(self, side):
        super().__init__()
        self.side = side

    def area(self):
        return self.side * self.side

    @property
    def diagonal(self):
        return math.sqrt(2) * self.side


@cache
def total_area(shapes):
    def safe(shape):
        try:
            return shape.area()
        except NotImplementedError:
            return 0
    return sum(safe(s) for s in shapes)


async def render(shapes):
    for shape in shapes:
        print(shape.describe())
    return total_area(tuple(shapes))
