# ResQNet

**ResQNet** — платформа для екстрених служб, яка автоматично виявляє стихійні лиха
за супутниковими даними, оголошує тривогу та сповіщає про неї диспетчерів і
населення у постраждалих районах.

## Загальна архітектура

Система складається з чотирьох частин:

```
                    ┌──────────────────────┐
                    │      Супутникові     │
                    │        дані          │
                    └──────────┬───────────┘
                               │
                               ▼
┌──────────────┐        ┌────────────────────────────────────── ┐
│   Frontend   │◄──────►│                Backend                │
│ (React/Vite) │  HTTP  │  ┌───────────┐ ┌───────┐ ┌──────────┐ │
└──────────────┘        │  │ ingestion │→│ alarm │→│ notific. │ │
                        │  └───────────┘ └───────┘ └────┬─────┘ │
┌──────────────┐        └───────────────────────────── ─┼─────── ┘
│   Мобільний   │◄──── сповіщення ──────────────────────┘
│  застосунок   │                   │
└──────────────┘                    ▼
                            ┌──────────────┐
                            │   PostGIS    │
                            │  (PostgreSQL │
                            │  + геодані)  │
                            └──────────────┘
```

| Компонент | Технологія | Призначення |
|-----------|------------|-------------|
| **Frontend** | React 18 + Vite | Вебінтерфейс диспетчера: карта, перегляд лих, тривог та районів |
| **Backend** | FastAPI + SQLAlchemy + GeoAlchemy2 | REST API та три сервіси обробки даних |
| **База даних** | PostGIS (PostgreSQL 16 + 3.4) | Зберігання сутностей та геопросторових даних |
| **Мобільний застосунок** | — | Отримання сповіщень про тривогу кінцевими користувачами |

## Backend

Бекенд написаний на **FastAPI** і поділений на три сервіси, які утворюють
послідовний конвеєр обробки даних.

### 1. `ingestion` — завантаження та аналіз даних

Головний вхідний сервіс. Відповідає за весь шлях від супутникових даних до
рішення про тривогу:

1. **Завантажує** дані зі супутника.
2. **Аналізує** їх, щоб виявити ознаки стихійного лиха.
3. За потреби **оголошує тривогу**, передаючи керування сервісу `alarm`.

### 2. `alarm` — оголошення тривоги

Сервіс, який фіксує факт тривоги: створює відповідний запис про лихо та
постраждалі райони в базі даних і запускає процес сповіщення.

### 3. `notification` — сповіщення клієнтів

Сервіс, який надсилає клієнтам (диспетчерам і користувачам мобільного застосунку)
сповіщення про тривогу. Способи доставки визначаються **нотіфаєрами**
(*notifiers*) — кожен нотіфаєр реалізує окремий канал сповіщення (наприклад
push-повідомлення, SMS тощо). Склад активних нотіфаєрів задає, якими саме
способами буде розіслано тривогу.

## Модель даних

Сутності описані в [`backend/models.py`](backend/models.py) через SQLAlchemy
(геопросторові поля — через GeoAlchemy2, система координат **SRID 4326 / WGS84**):

- **`users`**, **`user_roles`** — користувачі та їхні ролі (`dispatcher`, `mobile_user`).
- **`disaster_types`**, **`disasters`** — типи лих та зафіксовані лиха.
- **`disaster_areas`** — постраждалі райони (`POLYGON`), прив'язані до лиха.
- **`settlements`** — населені пункти з населенням і межами (`POLYGON`).
- **`roads`** та **`road_end1_connections` / `road_end2_connections`** — дорожня
  мережа: кожна дорога — це відрізок (`LINESTRING`) із двома кінцями (`POINT`),
  а зв'язки описують, які дороги зустрічаються на кожному з кінців.

Таблиці створюються автоматично під час старту бекенду
(`init_db()` у [`backend/database.py`](backend/database.py), що викликається
з `lifespan` у [`backend/main.py`](backend/main.py)). Розширення PostGIS
вмикається образом `postgis/postgis` під час ініціалізації бази.

### Map data: flood and roads (EMSR444 AOI02)

`init_db()` also upgrades a database created by an older `models.py` in place
(missing nullable columns and indexes), so an old `pgdata` volume keeps
working. Load the data from `backend/`:

```bash
export EMSR_ZIP=/path/to/EMSR444_AOI02_DEL_PRODUCT_r1_RTP01_v1_vector.zip
python -m service.ingestion_flood     # flood -> disaster_areas, AOI + Not Analysed -> analysis_coverage
python -m service.ingestion_roads     # OSM roads -> roads (cache dir: OSM_CACHE_DIR)
```

Flood data are append-only: an import adds rows and never overwrites
earlier ones, and importing the same product again writes nothing. Every row
has `source`, `product`, `observed_at` (image time, not load time),
`valid_until` (24 h after the image; `SATELLITE_MAX_AGE_HOURS` can shorten it)
and `ingested_at`. `service.ingestion_flood.observations_valid_at(session, at)`
returns what counts at moment `at` (`observed_at <= at < valid_until`); pass
`2020-06-26T12:00Z` to replay EMSR444. Whatever it does not return is unknown,
never safe. Details:
[`backend/SCHEMA_PROPOSAL_disaster_areas.md`](backend/SCHEMA_PROPOSAL_disaster_areas.md).

## Запуск

Увесь стек піднімається через Docker Compose:

```bash
docker compose up -d --build
```

Після запуску будуть доступні:

| Сервіс | Адреса |
|--------|--------|
| Frontend | http://localhost:5173 |
| Backend (API) | http://localhost:8000 |
| Health-check | http://localhost:8000/api/health |
| PostGIS | localhost:5432 (користувач/пароль/БД: `resqnet`) |

Бекенд під'єднується до бази за змінною середовища `DATABASE_URL`
(значення для Compose задано в [`docker-compose.yml`](docker-compose.yml)).

## Маршрутизація евакуації

Ендпоінт `POST /evacuation-route` (тіло `{lat, lon, at?}`, `at` з `Z` або зсувом) розраховує маршрут від найближчої дороги до точки евакуації на момент `at`:
- Кожна дорога має статус на момент `at`: `closed` (перетинає актуальну воду або є автентифікований OSNMA звіт «перекрита/затоплена», `reason = field_report`), `mapped_option` («не перевірено»: в актуальному аналізі води не видно), `unknown` (немає актуальних даних: `gap`, `expired`, `error`). Статусу «безпечно» немає.
- Дейкстра спершу шукає шлях тільки по `mapped_option`, потім дозволяє `unknown`; `closed` не використовується ніколи (крім стартової ділянки, з якої треба вийти).
- Статус маршруту = статус найслабшої ділянки; `limiting_segment_ids` показують, що перевірити. `route` - GeoJSON, статус у `properties` кожної ділянки.
- Якщо шляху немає: найближча суха `EvacuationPoint` або кандидат (кінець дороги поза водою), `route_status = unknown`, `reason = error`.

Детальніше про архітектурні рішення див. у [`brain/`](brain/README.md).

## Карта в реальному часі: `GET /api/map/live`

Один GeoJSON `FeatureCollection` з усім, що потрібно мапі диспетчера. У кожного об'єкта є `properties.kind`:

| kind | геометрія | що в properties |
|------|-----------|-----------------|
| `disaster_area` | Polygon | `name`, `disaster_id`, `disaster_name`, `since` (час знімка або `null`), `untimed` |
| `road` | LineString | `route_status` (`closed` / `mapped_option` / `unknown`), `reason`; `flooded` = `route_status == closed` (лише для сумісності, не для кольору) |
| `evacuation_point` | Point | `name`, `in_danger` - точка всередині зони лиха |
| `settlement` | Polygon | `name`, `population`, `affected` |
| `field_report` | Point | `road_id`, `status`, `observed_at`, `osnma_sample`, `osnma_verdict` (`authenticated` / `rejected` / `not_checked`) |

Блок `summary` - цифри для панелі: `roads_by_status`, скільки людей у зоні, які точки евакуації під загрозою, `observations` (джерело, `observed_at`, `valid_until` для кожного актуального знімка), `reports_total`, `reports_authenticated`, `reports_rejected`.

Параметри:
- `at` - стан на певний момент (ISO 8601 з `Z` або зсувом, інакше 400). Для демо: `2020-06-27T00:00:00Z`, той самий `at` у карті й маршруті.
- `disaster_id` - лише одне лихо.
- `bbox=minLon,minLat,maxLon,maxLat` - лише те, що видно на екрані.

Фронт оновлює мапу, запитуючи ендпоінт раз на кілька секунд.

## Польові звіти і Galileo OSNMA: `/api/reports`

**Що таке OSNMA.** Galileo OSNMA (Open Service Navigation Message Authentication) - безкоштовна
функція відкритого сервісу Galileo. Супутники разом із навігаційними даними (ефемериди, поправки
годинника) передають криптографічні мітки (TESLA), а приймач перевіряє їх відкритим ключем
Європейського центру GNSS-сервісів (GSC). Якщо мітка зійшлася, дані справді від Galileo, а не
підроблені.

**Навіщо це в Україні.** Глушіння і підміна (spoofing) GNSS-сигналу в Україні та біля неї - щоденна
реальність. Підроблений сигнал змушує телефон чи трекер «бачити» себе в іншому місці. Для ResQNet
це означає: звіт про дорогу може прийти з чужими координатами. OSNMA дає спосіб відрізнити
навігаційні дані Galileo від підробки.

**Як це працює в демо.**

- `POST /api/reports` - створити звіт: `point` (WKT `POINT(lon lat)`), `status`
  (`passable` | `blocked` | `flooded`), необов'язкові `road_id`, `observed_at` (з `Z` або зсувом)
  і `osnma_sample`.
- `osnma_sample` - який записаний зразок навігаційних даних перевірити:
  - `ublox_authentic` - справжній запис приймача u-blox від 09.03.2026 (з репозиторію OSNMAlib);
  - `forged` - той самий запис, де в ефемеридах трьох супутників (E03, E07, E08) перевернуто по
    одному біту (M0). Так виглядає підміна даних атакувальником.
- Бекенд проганяє запис через [OSNMAlib](https://github.com/Algafix/OSNMA) v3.2.5 (KU Leuven,
  EUPL-1.2) з публічним ключем GSC і повертає `osnma_verdict`:
  - `authenticated` - усі мітки зійшлися (14 супутників, 0 помилок);
  - `rejected` - OSNMAlib знайшла мітки, що не сходяться (у `forged`: саме E03, E07, E08);
  - `not_checked` - зразок не передали або перевірка недоступна.
  У `osnma_detail` - які супутники автентифіковано, які ні, приклади хибних міток, час GST і позначка
  `"source": "recorded sample"`.
- `GET /api/reports` - усі звіти.
- Вплив на статус дороги (`services/road_status.py`, той самий для мапи і маршруту): якщо останній
  **автентифікований** звіт про дорогу на момент `at` - `blocked` чи `flooded`, дорога стає
  `closed` з `reason = field_report`, і маршрут її обходить. Пізніший автентифікований `passable`
  знімає лише це польове закриття. Звіт ніколи не відкриває дорогу: вода на актуальному знімку
  лишається `closed`, а без знімка дорога лишається `unknown`. Звіти `rejected` і `not_checked`
  видно на мапі, але на статус вони не впливають.

Перевірка одного запису займає кілька секунд. Щоб API відповідав миттєво, вердикти обох зразків
рахуються у фоні під час старту бекенду і кешуються (у пам'яті та в JSON у `OSNMA_CACHE_DIR`).
OSNMAlib немає на PyPI, тому `backend/Dockerfile` завантажує архів тегу v3.2.5 (з перевіркою SHA-256)
і кладе його в `PYTHONPATH`. Дані і ліцензія: [`backend/data/osnma/`](backend/data/osnma/ATTRIBUTION.md).

**Обмеження.**

- OSNMA автентифікує **навігаційні дані**, а не позицію людини. Повтор справжнього сигналу (meaconing)
  чи глушіння OSNMA сам не виявляє, а `point` у звіті надсилає клієнт, і OSNMA його не підтверджує.
  Тому звіт може лише закрити дорогу, але не відкрити.
- У демо перевіряються **записані зразки**, а не живий приймач.
## Скільки людей у зонах повені: `GET /api/disasters/{id}/people-estimate`

Населення кожного населеного пункту вважаємо рівномірно розподіленим по його полігону. Зона повені отримує `population × частка площі пункту під водою`; площі рахуються в м² (geography), не в градусах. Кожна зона рахується окремо (`zones[]`, з розбивкою по пунктах), а `total_people` береться з об'єднання зон, тож село під двома зонами не рахується двічі.

Зона враховується на момент `at`, якщо це супутникове спостереження і воно ще актуальне (`observed_at <= at < valid_until`), або якщо її намалював диспетчер (без `observed_at`) і лихо вже почалося. 404, якщо лиха немає.

## Тести

Тести ганяються в контейнері, бо на Windows порт 5432 часто зайнятий локальним PostgreSQL:

```
docker compose run --rm -e TEST_ADMIN_URL=postgresql+psycopg2://resqnet:resqnet@db:5432/resqnet backend sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q tests"
```
