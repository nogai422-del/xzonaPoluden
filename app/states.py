from aiogram.fsm.state import State, StatesGroup


class RegisterPlayer(StatesGroup):
    nickname = State()


class AddItem(StatesGroup):
    player = State()
    name = State()
    quantity = State()
    comment = State()
    confirm = State()


class EditItem(StatesGroup):
    name = State()
    quantity = State()
    comment = State()
