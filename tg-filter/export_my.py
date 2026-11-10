"""
Выгружает все каналы и чаты, на которые ты подписан, в файл my_subs.txt
(формат как у input.txt — его можно сразу подать в prefilter.py или tgfilter.py).

Один запрос списка диалогов — лимиты аккаунта почти не тратит.
Не запускай одновременно с tgfilter.py (они используют одну сессию).

Запуск:  python3.11 export_my.py
"""
import asyncio

from telethon import TelegramClient, types

from common import BASE, load_config

SESSION = str(BASE / "tgfilter")


async def main():
    api_id, api_hash, proxy = load_config()
    client = TelegramClient(SESSION, api_id, api_hash, proxy=proxy)
    await client.connect()
    if not await client.is_user_authorized():
        print("Сначала войди через tgfilter.py (он покажет QR-код).")
        return

    channels, chats, private = [], [], []
    async for d in client.iter_dialogs():
        e = d.entity
        if not isinstance(e, (types.Channel, types.Chat)):
            continue                      # личные переписки и боты не нужны
        is_channel = isinstance(e, types.Channel) and e.broadcast
        username = getattr(e, "username", None)
        if not username and getattr(e, "usernames", None):
            active = [u.username for u in e.usernames if u.active]
            username = active[0] if active else None
        line = f"https://t.me/{username}   # {d.name}" if username else None
        if line is None:
            private.append(f"{'канал' if is_channel else 'чат'}  id={d.id}  {d.name}")
        elif is_channel:
            channels.append(line)
        else:
            chats.append(line)

    out = ["# Мои подписки (выгружено export_my.py)", "",
           "[Крипто каналы]", *sorted(channels, key=str.lower), "",
           "[Крипто чаты]", *sorted(chats, key=str.lower), ""]
    (BASE / "my_subs.txt").write_text("\n".join(out), encoding="utf-8")
    (BASE / "my_private.txt").write_text(
        "# Приватные каналы/чаты без публичной ссылки. Ты в них состоишь,\n"
        "# поэтому позже их можно будет мониторить по id.\n\n" + "\n".join(private), encoding="utf-8")
    await client.disconnect()
    print(f"Публичных каналов: {len(channels)}, чатов: {len(chats)} → my_subs.txt")
    print(f"Приватных без ссылки: {len(private)} → my_private.txt")


if __name__ == "__main__":
    asyncio.run(main())
