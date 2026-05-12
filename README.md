# AI News Bot

Telegram boti — sun'iy intellekt sohasidagi eng dolzarb yangiliklar

---

## Asosiy xususiyatlar

| Xususiyat | Tavsif |
|---|---|
| 🌐 Ko'p tilli | O'zbek · Русский · English |
| 📡 RSS manbalari | Google News · Reddit r/artificial · r/MachineLearning |
| 🧠 Claude AI | Yangiliklar tahlili, tarjimasi va muhimlik reytingi |
| 🔔 Avtomatik yuborish | Har 5 soatda / Har kuni / Har 3 kunda |
| 💾 SQLite | Har bir foydalanuvchi uchun alohida sozlamalar |
| ♻️ Duplikat filtri | Bir hafta ichida takroriy yangiliklar yo'q |

---

## Talablar

- Python **3.11** yoki yangi versiyasi
- Telegram Bot Token (BotFather orqali)
- Anthropic API Key

---

## 1-qadam — Bot token olish (Telegram BotFather)

1. Telegramda **[@BotFather](https://t.me/BotFather)** ga oching.
2. `/newbot` buyrug'ini yuboring.
3. Bot uchun **ism** kiriting (masalan: `AI News Bot`).
4. Bot uchun **username** kiriting, oxiri `bot` bilan tugashi kerak (masalan: `my_ainews_bot`).
5. BotFather sizga **token** beradi. Uni saqlang:
   ```
   1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
   ```

---

## 2-qadam — Anthropic API Key olish

1. **[console.anthropic.com](https://console.anthropic.com)** ga kiring.
2. Ro'yxatdan o'ting yoki tizimga kiring.
3. Chap menyu → **API Keys** → **Create Key** tugmasini bosing.
4. Kalitga nom bering va yarating.
5. Kalitni nusxa oling — u faqat bir marta ko'rsatiladi:
   ```
   sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXX
   ```

---

## 3-qadam — O'rnatish

### Kodni yuklab olish

```bash
# Git bilan (agar repoda bo'lsa)
git clone https://github.com/your-username/ai-news-bot.git
cd ai-news-bot

# Yoki fayllarni yuklab olgan bo'lsangiz, papkaga o'ting
cd ai-news-bot
```

### Virtual muhit yaratish (tavsiya etiladi)

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### Kutubxonalarni o'rnatish

```bash
pip install -r requirements.txt
```

---

## 4-qadam — Sozlash

`.env.example` faylini nusxa oling va to'ldiring:

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

`.env` faylini matn muharririda oching va qiymatlarni kiriting:

```ini
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
ANTHROPIC_API_KEY=sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

---

## 5-qadam — Ishga tushirish

```bash
python bot.py
```

Muvaffaqiyatli ishga tushganda konsolda quyidagilar ko'rinadi:

```
2026-05-12 10:00:00 | __main__ | INFO | Starting AI News Bot…
2026-05-12 10:00:01 | __main__ | INFO | Database initialised successfully at 'users.db'
2026-05-12 10:00:01 | __main__ | INFO | Re-scheduled jobs for 0 existing users
2026-05-12 10:00:01 | __main__ | INFO | Bot is running — press Ctrl+C to stop
```

Botni to'xtatish uchun: **Ctrl + C**

---

## Fayl tuzilmasi

```
ai-news-bot/
├── bot.py           — Asosiy bot: handlers, keyboard, scheduler
├── news.py          — RSS olish + Claude AI tarjimasi
├── db.py            — SQLite: foydalanuvchi sozlamalari, duplikat filtr
├── requirements.txt — Python kutubxonalari
├── .env.example     — Namuna konfiguratsiya
├── .env             — Haqiqiy kalit (gitga qo'shilmaydi!)
├── users.db         — Baza fayli (avtomatik yaratiladi)
└── bot.log          — Log fayli (avtomatik yaratiladi)
```

---

## Windows — Avtomatik ishga tushirish (Task Scheduler)

Kompyuter yoqilganda bot avtomatik ishga tushishi uchun:

### Usul 1 — GUI orqali

1. **Win + R** → `taskschd.msc` → Enter
2. O'ng panelda **"Create Task"** ni bosing
3. **General** tab:
   - Name: `AI News Bot`
   - ✅ `Run whether user is logged on or not`
   - ✅ `Run with highest privileges`
4. **Triggers** tab → **New**:
   - Begin the task: `At startup`
   - Delay task for: `30 seconds` (tarmoq ulangunga kutish)
5. **Actions** tab → **New**:
   - Action: `Start a program`
   - Program/script: `C:\Users\YourName\ai-news-bot\venv\Scripts\python.exe`
   - Add arguments: `bot.py`
   - Start in: `C:\Users\YourName\ai-news-bot`
6. **Settings** tab:
   - ✅ `If the task fails, restart every:` 1 minute, up to 3 times
7. **OK** → Windows parolingizni kiriting → **OK**

### Usul 2 — CMD orqali (Administrator sifatida)

Quyidagi buyruqni **Administrator** sifatida ishga tushirilgan CMD da bajaring
(yo'llarni o'zingiznikiga moslashtiring):

```bat
schtasks /create ^
  /tn "AI News Bot" ^
  /tr "\"C:\Users\YourName\ai-news-bot\venv\Scripts\python.exe\" \"C:\Users\YourName\ai-news-bot\bot.py\"" ^
  /sc ONSTART ^
  /delay 0000:30 ^
  /ru SYSTEM ^
  /f
```

### Tekshirish

```bat
schtasks /query /tn "AI News Bot"
```

### O'chirish (kerak bo'lsa)

```bat
schtasks /delete /tn "AI News Bot" /f
```

---

## Xatolarni bartaraf etish

### `ModuleNotFoundError`
```bash
# Virtual muhit faol ekanligini tekshiring
venv\Scripts\activate
pip install -r requirements.txt
```

### `BOT_TOKEN is not set`
- `.env` fayli bot.py bilan bir papkada ekanligini tekshiring
- `.env` ichidagi qiymatlar to'g'ri ekanligini tekshiring

### `Anthropic API error 401`
- `ANTHROPIC_API_KEY` ni tekshiring — kalitda yetarli kredit borligini [console.anthropic.com](https://console.anthropic.com) da ko'ring

### `Conflict: terminated by other getUpdates request`
- Bot boshqa joyda ham ishga tushirilgan. Barcha nusxalarni to'xtating, keyin bitta ishga tushiring.

### RSS yangiliklari kelmayapti
- Internet aloqasini tekshiring
- Reddit ba'zan bot so'rovlarini bloklashi mumkin — bu normal, Google News zahira sifatida ishlaydi

---

## Litsenziya

MIT — erkin foydalaning va o'zgartiring.
