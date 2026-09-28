from . import models
from .models import User
from ..shared import util
import os


def create(name):
    user = User(name)
    models.User(name)
    util.log(user.display())
    return os.getenv("MODE", "dev")
