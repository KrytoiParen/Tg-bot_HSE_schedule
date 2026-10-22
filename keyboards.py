from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def groups(labels, nonce):
    rows = []
    for i, label in enumerate(labels):
        rows.append([InlineKeyboardButton(text=label[:100], callback_data=f"pick:{nonce}:{i}")])
    rows.append([InlineKeyboardButton(text="✖️ Отменить", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


main_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="today"),
            InlineKeyboardButton(text="Завтра", callback_data="tomorrow"),
        ],
        [InlineKeyboardButton(text="Ближайшие 7 дней", callback_data="week")],
        [
            InlineKeyboardButton(text="⚙️ Учебная группа", callback_data="settings"),
            InlineKeyboardButton(text="Английский", callback_data="english"),
        ],
        [
            InlineKeyboardButton(text="Время рассылки", callback_data="time"),
            InlineKeyboardButton(text="Состояние", callback_data="status"),
        ],
        [InlineKeyboardButton(text="Отключить рассылку", callback_data="stop")],
    ]
)
