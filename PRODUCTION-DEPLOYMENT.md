# Wanas Gallery — خطة النزول Production

> **ملاحظة:** بعد كتابة هذا الملف اتشال `dashboard/` و`storefront/` و`web/` من الريبو نهائيًا — المتجر بقى على Shopify theme، والداشبورد بقى Shopify Admin. الفقرات اللي بتتكلم عن `/store` أو `/dashboard` كمسارات في نفس الـ app بقت مش دقيقة، انظر `CLAUDE.md`.

المشروع (backend + bot) app واحد FastAPI (modular monolith)، فالنزول production بسيط نسبيًا: نستضيف الـ app على Railway بـ Postgres، ونوجّه دومين اشتريته من Shopify على نفس الاستضافة، ونربط WhatsApp عن طريق Meta.

## 0. قبل ما تبدأ

- الكود لازم يبقى على GitHub (Railway بيدبلوي مباشرة من repo). لو لسه مش عليه:
  ```bash
  git init && git add . && git commit -m "initial"
  # اعمل repo على GitHub وارفعه
  ```
- تأكد إن `.env` و `wanas.db*` و `test_wanas.db` مش متتبعين في git (شوف `.gitignore`، المفروض already covered).

## 1. الداتابيز — من SQLite لـ Postgres

1. على Railway: `New Project` → `Provision PostgreSQL`. هيديك `DATABASE_URL` جاهز في الـ variables بتاعة السيرفس ده.
2. الكود مبني من الأول عشان يقبل الاتنين — السطر اللي بيتغير بس هو:
   ```
   DATABASE_URL=postgresql+psycopg://user:pass@host:port/db
   ```
   (لاحظ لازم يبقى `postgresql+psycopg://` مش `postgresql://` عادي، عشان SQLAlchemy يستخدم driver psycopg 3 اللي زودته في requirements.txt).
3. مفيش Alembic في المشروع — الجداول بتتعمل بـ `Base.metadata.create_all(engine)` في startup (`app.py`). يعني أول تشغيل هيعمل الجداول لوحده. بعد كده:
   ```bash
   python -m backend.cli seed
   python -m backend.cli set-fee <المحافظة> <الرسوم>   # لكل الـ 27 محافظة
   python -m backend.cli create-staff <username>
   ```
   دول لازم تتشغل مرة واحدة على الداتابيز الجديدة (تقدر تشغلهم من Railway shell/CLI أو تعمل `railway run` لو نازل الـ CLI بتاعهم محليًا).
4. اختياري بس منصوح بيه قبل الإطلاق: شغّل التستات على Postgres نفسها قبل ما تعتمد عليها:
   ```bash
   DATABASE_URL=postgresql+psycopg://... python -m pytest tests/ -q
   ```

## 2. رفع الـ app على Railway

1. Railway → `New Service` → `Deploy from GitHub repo` (اختار الـ repo).
2. Start command (Railway بيكتشف تلقائي غالبًا، لو مش هتأكد حطه في `railway.json` أو Settings):
   ```
   uvicorn app:app --host 0.0.0.0 --port $PORT
   ```
3. Environment variables — انسخ كل حاجة من `.env.example` واملاها فعليًا في Railway (Settings → Variables)، أهمهم:
   - `DATABASE_URL` (من خطوة 1)
   - `LLM_PROVIDER=gemini` + `LLM_API_KEY` + `LLM_MODEL` (لو عايز تثبته)
   - `HARNESS_ENABLED=0` ← **مهم جدًا** — الـ harness endpoint مش محمي بأي login، لازم يتقفل قبل ما حد غيرك يوصله.
   - `CHATBOT_DEBUG=0` — عشان أخطاء الـ provider الخام ما تظهرش للعميل.
   - `WHATSAPP_*` (هتملاها في خطوة 4).
4. Deploy، وبعدها Railway بيديك رابط مؤقت زي `xxx.up.railway.app` — افتح `/health` عليه اتأكد إن `status: ok` وإن الاتصال بالداتابيز شغال.

## 3. الدومين من Shopify → Railway

Shopify بيبيع دومينات كـ registrar عادي، مش شرط تستخدم متجر Shopify نفسه — تقدر توجه الدومين لأي استضافة تانية زي Railway بسهولة.

1. اشتري الدومين من Shopify (Settings → Domains → Buy new domain، أو من موقع Shopify Domains مباشرة لو مفيش متجر شوبيفاي أصلاً).
2. على Railway: السيرفس بتاعك → Settings → Networking → `Add Custom Domain`، اكتب الدومين. Railway هيديك target (CNAME عادةً).
3. رجع لإعدادات الدومين في Shopify → DNS settings، وضيف:
   - CNAME record بيوجه للـ target اللي Railway ديهولك (أو A record لو Railway طلب كده).
4. استنى الـ DNS يتنشر (دقايق لحد كام ساعة). Railway بيصدر شهادة SSL تلقائي (Let's Encrypt) بمجرد ما الـ DNS يتأكد.
5. المتجر نفسه بقى Shopify theme على دومين المتجر مباشرة (`admin.shopify.com` / الدومين اللي هيتربط بالمتجر) — مش جزء من الـ FastAPI app دي خالص. الدومين اللي بتوجهه لـ Railway ده بس لـ webhook الواتساب والـ API، مفيش `/store` تاني في الـ app.

## 4. Meta / WhatsApp Business API

1. Meta for Developers → أنشئ App → ضيف منتج WhatsApp.
2. من إعدادات الـ WhatsApp product هتاخد: `Phone Number ID`، `App Secret`. لازم تعمل System User واخد منه Access Token دائم (مش الـ token المؤقت اللي بينتهي بعد 24 ساعة).
3. اختار `WHATSAPP_VERIFY_TOKEN` بنفسك (أي string)، وحطه في متغيرات Railway وفي إعدادات الـ webhook على Meta.
4. سجّل الـ webhook:
   - URL: `https://yourdomain.com/webhooks/whatsapp`
   - الكود already بيرد على الـ GET verification handshake تلقائي، ومحتاج الدومين شغال بـ HTTPS الأول (يعني بعد ما تخلص خطوة 3).
   - Subscribe على field اسمه `messages`.
5. **Templates**: أي رسالة proactive (تأكيد أوردر، تحديث حالة، طلب تقييم) لازم template متوافق عليه من Meta الأول — الموافقة ممكن تاخد أيام لأسابيع. لحد ما توافق عليهم بيتبعتوا كـ free-form text وده بيشتغل بس مع أرقام test موثقة فقط.

## 5. حاجات أمان لازم تتعمل قبل الإطلاق الفعلي

- `HARNESS_ENABLED=0` (اتقال فوق بس بيتكرر لأهميته)
- `CHATBOT_DEBUG=0`
- رسوم الشحن لكل الـ 27 محافظة متظبطة
- Meta templates متوافق عليها

## ترتيب التنفيذ المقترح

1. رفع الكود على GitHub
2. Railway: مشروع جديد + Postgres + رفع الـ env vars + deploy، تتأكد من `/health`
3. تشغيل `seed` / `set-fee` / `create-staff` على الداتابيز الجديدة
4. شراء الدومين من Shopify وربطه بـ Railway (custom domain + DNS)، التأكد إن SSL شغال
5. تسجيل Meta app، ربط الـ webhook بالدومين، طلب موافقة الـ templates
6. اختبار end-to-end بأرقام test، وبعدين `HARNESS_ENABLED=0` قبل أي إطلاق فعلي للعموم
