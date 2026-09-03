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


class TelethonSetup(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


class MarketSettings(StatesGroup):
    merchant_target = State()


class MarketOrder(StatesGroup):
    item_name = State()
    quantity = State()
    comment = State()


class GroupAddItem(StatesGroup):
    name = State()
    quantity = State()
    comment = State()
    confirm = State()


class GroupMarketOrder(StatesGroup):
    item_name = State()
    quantity = State()
    comment = State()


class GroupMarketSettings(StatesGroup):
    merchant_target = State()

class GroupGpStock(StatesGroup):
    item_name = State()
    quantity = State()


class GroupEventCreate(StatesGroup):
    title = State()
    starts_at = State()
    capacity = State()
    details = State()


class GroupDiplomacy(StatesGroup):
    faction = State()
    relation = State()
    note = State()


class GroupTargetCreate(StatesGroup):
    name = State()
    reason = State()
    reward = State()
    location = State()


class GroupInfoCreate(StatesGroup):
    title = State()
    body = State()


class GroupRoleAdmin(StatesGroup):
    search = State()
