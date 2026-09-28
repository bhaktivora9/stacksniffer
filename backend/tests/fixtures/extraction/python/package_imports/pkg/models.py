class User:
    def __init__(self, name):
        self.name = name

    def display(self):
        return self.name.title()
