# Trend Pullback Bot (Paper Trading) - OKX

بوت بيدخل ويخرج لوحده بفلوس وهمية (1000$)، وبيبعت كل حركة على تيليجرام. مفيش API keys ولا فلوس حقيقية.

## 1) تيليجرام
1. @BotFather -> /newbot -> انسخ الـ token.
2. ابعت أي رسالة للبوت بتاعك.
3. افتح `https://api.telegram.org/bot<TOKEN>/getUpdates` وانسخ `chat.id`.

## 2) Backtest الأول (على جهازك)
```
pip install -r requirements.txt
python bot.py backtest --days 180
```
بيطلع عدد الصفقات، نسبة الربح، Profit Factor، أقصى سحب. لو الصفقات قليلة جدًا، ده معناه إن فلتر التكلفة بيرفض معظم الإشارات
(BTC/ETH على 15 دقيقة حركتهم صغيرة بالنسبة للرسوم) - ابعت النتيجة ونعدل.
الـ Backtest مش بيشمل الـ Daily Assessment لأن funding / order book / الأخبار مفيهمش تاريخ.

## 3) التشغيل على GitHub Actions
1. اعمل repo **Public** (الـ Private حده 2000 دقيقة/شهر ومش هيكفي).
   تنبيه: ملفات `data/` (الرصيد والصفقات) هتبقى ظاهرة للكل.
2. ارفع: `bot.py`, `requirements.txt`, `events.txt`.
3. ارفع `bot.yml` في المسار ده بالظبط: `.github/workflows/bot.yml`
   (Add file -> Create new file -> اكتب المسار في الاسم).
4. Settings -> Secrets and variables -> Actions -> New repository secret:
   `TELEGRAM_TOKEN` و `TELEGRAM_CHAT_ID`.
5. Settings -> Actions -> General -> Workflow permissions -> **Read and write permissions**.
6. تبويب Actions -> paper-bot -> **Run workflow** (تجربة يدوية). لو فشل بسبب الحجب الجغرافي (OKX من سيرفرات أمريكا)،
   شوف الـ log وقولي.
7. بعد كده بيشتغل لوحده كل 10 دقائق تقريبًا (GitHub ممكن يتأخر أو يتخطى تشغيلات).
   GitHub بيوقف الـ schedule لو الـ repo ما فيهوش نشاط 60 يوم.

## 4) الـ Daily Trading Assessment (اختبار صلاحية اليوم)
بيتحسب في كل تشغيلة، وتقرير كامل مرة كل يوم (أول تشغيلة بعد 00:00 UTC)، ورسالة لو القرار اتغير:

| البند | النقاط | المصدر |
|---|---|---|
| Market Structure | 20 | BTC على 1D و 4H |
| Volatility | 10 | ATR لـ BTC 15m |
| Liquidity | 10 | spread دفتر الأوامر (BTC/ETH/SOL) |
| Trading Volume | 10 | حجم BTC آخر 24 ساعة مقابل متوسط 7 أيام |
| Technical Opportunities | 20 | توافق الفلاتر + pullback شغال |
| Derivatives & Sentiment | 10 | Funding Rate فقط (**Open Interest مش داخل**) |
| News & Economic Risk | 10 | ملف `events.txt` (انت اللي بتملاه) |

- أي بند بياناته مش متاحة **مش بياخد نقاط**، والدرجة بتتحسب على المتاح وتتعلّم INCOMPLETE، واليوم ما يوصلش TRADE كامل.
- القرار: 80+ TRADE (مخاطرة x1) | 65-79 SELECTIVE (x0.75) | 50-64 HIGH CAUTION (x0.5) | أقل من 50 NO TRADE (مفيش دخول جديد).
- شروط منع إلزامية (NO TRADE مهما كانت الدرجة): مفيش اتجاه واضح لـ BTC، spread عالي، خبر مؤثر خلال 60 دقيقة،
  بيانات أساسية مش متاحة، أو حدود الخسارة/الصفقات اليومية اتوصلت.
- الصفقات المفتوحة بتتدار دايمًا (SL/TP) حتى لو اليوم NO TRADE.

### events.txt
مرة في الأسبوع: شوف تقويم اقتصادي، اكتب تواريخ الأخبار الكبيرة (UTC)، واكتب تاريخ النهاردة قدّام `checked:`.
لو عدّى 7 أيام من غير تحديث، بند الأخبار بيرجع "غير متاح".

## الاستراتيجية
- فلاتر: ترند 4H (السعر و EMA50 مقابل EMA200)، BTC 1H مقابل EMA50، نطاق ATR، حجم أعلى من المتوسط.
- دخول (إغلاق شمعة 15M): pullback لـ EMA20، RSI ينزل تحت 42 ويقفل فوق 50 (عكسه للـ SHORT).
- SL: خلف Swing أو 1.5 ATR (الأبعد). TP1 عند 1R (50%، والـ SL ينتقل للدخول)، TP2 عند 2R (25%)، والباقي Trailing على EMA20 أو 3R.
- مخاطرة 0.75% للصفقة (نص في الويك إند)، أقصى 3 صفقات/يوم، توقف بعد خسارتين ورا بعض أو -2% في اليوم، أقصى صفقتين مفتوحتين، Time stop بعد 16 ساعة.
- الرسوم المفترضة: 0.05% لكل جانب (Futures taker) + 0.02% slippage. تمويل الـ Perpetual (funding) مش محسوب.

## قبل أي فلوس حقيقية
- Backtest: 100+ صفقة، Profit Factor فوق 1.3، أقصى سحب تحت 10%، على فترات مختلفة.
- Paper trading من 4 لـ 8 أسابيع بنتائج قريبة من الـ Backtest.
- بعدها: رأس مال صغير جدًا، و API key بصلاحية تداول فقط (من غير سحب).
