from __future__ import annotations

from html import escape
from io import BytesIO

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image, ImageDraw, ImageFont


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows if row
    ])


def storage_panel():
    return keyboard(
        [("📦 Остатки / заявка", "c8:catalog:request:0")],
        [("📋 Очередь заявок", "c8:requests:all:0"), ("👤 Мои заявки", "c8:requests:mine:0")],
        [("🛠 Управление складом", "c8:catalog:stock:0"), ("📜 История выдач", "c8:requests:history:0")],
        [("🗄 Старые записи хранения", "c8:legacy_storage")],
    )


def market_panel():
    return keyboard(
        [("➕ Создать объявление", "c8:catalog:sale:0")],
        [("🪧 Доска объявлений", "c8:ads:all:0"), ("👤 Мои объявления", "c8:ads:mine:0")],
    )


def diplomacy_panel():
    return keyboard(
        [("📊 Таблица отношений", "c8:matrix")],
        [("✏️ Изменить отношения", "c8:pair"), ("➕ Группировка", "c8:faction_add")],
        [("⚖️ Враги союзников", "c8:candidates:0"), ("📜 История", "c8:dip_history")],
    )


def request_text(r):
    labels = dict(pending="⏳ На одобрении Лидера / Заместителя", approved="✅ Одобрено — ожидает Кладовщика",
                  issued="📦 Выдано", rejected="⛔ Отклонено", cancelled="Отменено")
    text = (f"<b>📦 Заявка #{r['id']}</b>\n"
            f"<a href=\"tg://user?id={r['requester_id']}\">{escape(r['game_nickname'])}</a>\n"
            f"{escape(r['name'])} × {r['quantity']}\n{escape(r['reason'])}\n\n{labels[r['status']]}")
    if r.get('reviewed_by'):
        text += f"\nРешение: <a href=\"tg://user?id={r['reviewed_by']}\">руководитель</a>"
    if r.get('issued_by'):
        text += f"\nВыдал: <a href=\"tg://user?id={r['issued_by']}\">кладовщик</a>"
    return text


def request_keyboard(r):
    rid = r['id']
    buttons = []
    if r['status']=='pending':
        buttons.append([("✅ Одобрить", f"c8:req:approve:{rid}"), ("⛔ Отклонить", f"c8:req:reject:{rid}")])
    if r['status']=='approved':
        buttons.append([("📦 Подтвердить выдачу", f"c8:req:issue:{rid}")])
    if r['status'] in ('pending','approved'):
        buttons.append([("Отменить заявку", f"c8:req:cancel:{rid}")])
    return keyboard(*buttons) if buttons else None


def ad_text(r):
    label = {'active':'🟢 Продаётся','sold':'✅ Продано','removed':'Снято с продажи'}[r['status']]
    # User content is limited at entry so the plain caption stays below 1024.
    return (f"<b>🪧 Объявление #{r['id']} · {label}</b>\n<b>{escape(r['name'])}</b>\n"
            f"<b>{r['price']:,} ₽</b>\n\n{escape(r['description'])}\n\n"
            f"Продавец: <a href=\"tg://user?id={r['seller_id']}\">{escape(r['game_nickname'])}</a>")


def ad_keyboard(r):
    if r['status']!='active':
        return None
    return keyboard([("✅ Продано", f"c8:adclose:sold:{r['id']}"), ("Снять", f"c8:adclose:removed:{r['id']}")])


def _font(size):
    # Windows development and Debian/Ubuntu Docker runtime.
    for path in ('C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise RuntimeError('Не найден шрифт с кириллицей. Установите fonts-dejavu-core.')


def render_matrix(factions, relations):
    """PDA-inspired grid: numbered columns keep faction names readable on mobile."""
    count = len(factions)
    cell, left, top = 48, 270, 150
    width, height = left + cell * count + 36, top + cell * count + 90
    im = Image.new('RGB', (width,height), '#151b17')
    draw = ImageDraw.Draw(im)
    title, body, small = _font(30), _font(20), _font(17)
    draw.rectangle((14,14,width-14,height-14), outline='#68734e', width=2)
    draw.text((30,27), 'ПОЛДЕНЬ  /  ДИПЛОМАТИЯ', font=title, fill='#d5d3a1')
    draw.text((30,73), 'Отношения группировок • номер столбца = номер строки', font=small, fill='#a3aa91')
    mapping = {(r['a'],r['b']):r['relation'] for r in relations}
    colors = {'ally':'#7da85c','neutral':'#ded184','war':'#c36451',None:'#444f46'}
    symbols = {'ally':'+','neutral':'=','war':'×',None:'?'}
    for j in range(count):
        draw.text((left+j*cell+cell/2,top-27), str(j+1),font=body,anchor='mm',fill='#d5d3a1')
    for i,f in enumerate(factions):
        y = top+i*cell
        label = f"{i+1:02}  {f['name']}"
        while draw.textlength(label,font=body)>left-48:
            label = label[:-2]+'…'
        draw.text((30,y+cell/2),label,font=body,anchor='lm',fill='#e0e1c8')
        for j,other in enumerate(factions):
            x = left+j*cell
            relation = mapping.get(tuple(sorted((f['id'],other['id']))))
            fill = '#2b332a' if i==j else colors[relation]
            draw.rectangle((x+3,y+3,x+cell-3,y+cell-3), fill=fill)
            draw.text((x+cell/2,y+cell/2), '—' if i==j else symbols[relation], font=body, anchor='mm', fill='#101510' if relation else '#bcc2b2')
    draw.text((30,height-53), '+ Союз     = Нейтралитет     × Война     ? Нет данных',font=body,fill='#d5d3a1')
    stream = BytesIO()
    im.save(stream,format='PNG')
    return stream.getvalue()
