from aiogram.fsm.state import State, StatesGroup


class AddMonitor(StatesGroup):
    name = State()
    url = State()


class SetInterval(StatesGroup):
    seconds = State()
