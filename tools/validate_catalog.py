#!/usr/bin/env python3
"""CI-валидатор каталога WoSB (catalog.json + renames.json + texts.json).

Повторяет правила парсера приложения (CatalogParser, Kotlin,
app/src/main/java/.../data/catalog/CatalogFile.kt) и добавляет проверки,
которые в приложении выполняются во время расчёта: обнаружение циклов
в графе рецептов — порт DFS из CalcSession (тот же формат сообщения
«Обнаружен цикл в рецептах: A → B → A»),

С v0.2 (решения владельца): секция groups — группы являются данными
(id/emoji/title, назначаются админом), и журнал переименований
renames.json (append-only; устройство мигрирует пользовательские данные
по записям при «Обновить БД»). Секция groups отсутствует только в
легаси-файлах — тогда действует встроенный список 1/2/3 приложения.

С v0.3: формат 2 — ключ времени крафта переименован
craftTimeHours → craftTime («крафт-время» измеряется в единицах, это
расходник рецепта, а не часы; файлы формата 1 отклоняются), у
мануфактур обязателен булев флаг availableInDangerousWaters
(«доступна в опасных водах»: чекбокс ×1.25 в расчёте предлагается
только отмеченным мануфактурам — по образцу canUsePrisoners).

С v0.4 (решение владельца, wosb-android-43): группы получают kind
(resources / tools / ships; отсутствие — resources); у юнит-групп
(орудия/корабли) предметы — только крафт: без веса, без
крафт-времени, без мануфактуры и чужих источников, у кораблей выход
всегда 1 шт.; материалы рецептов орудий/кораблей обязаны иметь
флаг isToolMaterial / isShipMaterial (фильтр выбора в редакторе
приложения). Новые ключи опциональны: старые версии приложения
их молча пропускают (ignoreUnknownKeys).

С v0.5 (решение владельца, wosb-android-79): kind ammunition
(«Боеприпасы», seed-группа 4) — тоже только крафт, без мануфактуры
и чужих источников, но ВЕС и КРАФТ-ВРЕМЯ ЗАДАЮТСЯ (вес партии
нужен смете калькулятора), выход задаёт админ (батчевый); материалы
рецептов боеприпасов обязаны иметь флаг isAmmoMaterial
(«Материал для боеприпасов») — фильтр выбора в редакторе.

texts.json (wosb-android-75) — переопределения текстов интерфейса
приложения (заголовки/подсказки экранов, тексты настроек). Реестр
блоков (идентификаторы и дефолты) живёт в коде приложения
(AppTexts.kt); файл хранит ТОЛЬКО отличия, незнакомые идентификаторы
легальны (файл мог написать редактор более новой версии) — валидируется
только структура. Пустой файл переопределений — легальное начальное
состояние.

Выход: 0 — ошибок нет (предупреждения допустимы), 1 — есть ошибки.
Использование: python3 tools/validate_catalog.py catalog.json [renames.json] [texts.json]
"""

import json
import sys

# Константы — зеркало CatalogFile.kt / ManufactoryLevels.kt приложения.
APP_TAG = "wosb-catalog"
FORMAT_VERSION = 2          # формат 2: ключ крафт-времени — craftTime (v0.3)
OLD_TIME_KEY = "craftTimeHours"  # устаревший ключ формата 1 — отказ
LEVEL_COUNT = 4             # уровней у мануфактуры: либо 0, либо ровно 4
PRISONERS_ITEM = "Пленные"  # служебный предмет-ресурс пленных
KNOWN_GROUPS = {1, 2, 3}    # 1 «📦 Ресурсы», 2 «💣 Орудия», 3 «⛵ Корабли»

# Семантика групп (v0.4/v0.5): kind в секции groups; отсутствие — resources.
GROUP_KINDS = {"resources", "tools", "ships", "ammunition"}
# Только крафт (v0.4/v0.5): без мануфактуры и чужих источников.
CRAFT_ONLY_KINDS = {"tools", "ships", "ammunition"}
# Юнит-группы (v0.4): + без веса и крафт-времени.
UNIT_KINDS = {"tools", "ships"}
CRAFT_ONLY_TITLES = {"tools": "орудий", "ships": "кораблей", "ammunition": "боеприпасов"}
MATERIAL_FLAG = {
    "tools": "isToolMaterial",
    "ships": "isShipMaterial",
    "ammunition": "isAmmoMaterial",
}
FLAG_LABEL = {
    "tools": "«Материал для орудий»",
    "ships": "«Материал для кораблей»",
    "ammunition": "«Материал для боеприпасов»",
}

# Файл текстов интерфейса (wosb-android-75) — зеркало TextsFile.kt приложения.
TEXTS_APP_TAG = "wosb-texts"
TEXTS_FORMAT_VERSION = 1


def is_num(value):
    """Число (int/float), но не bool — в Python bool является int."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate(data):
    """Возвращает (errors, warnings) — список строк с сообщениями."""
    errors = []
    warnings = []

    # --- Шапка файла -------------------------------------------------
    if data.get("app") != APP_TAG:
        errors.append(f"Это файл другого приложения (app = «{data.get('app')}»)")
    fmt = data.get("formatVersion")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        errors.append(f"Версия формата {fmt!r} некорректна — ожидается целое число")
    elif fmt > FORMAT_VERSION:
        errors.append(
            f"Версия формата {fmt} выше поддерживаемой {FORMAT_VERSION} — обновите приложение"
        )
    elif fmt < FORMAT_VERSION:
        errors.append(
            f"Файл устаревшего формата {fmt}: репозиторий принимает только формат "
            f"{FORMAT_VERSION} (крафт-время хранится в ключе craftTime)"
        )

    # --- Секция groups (v0.2): группы — данные, назначает админ ------
    groups_raw = data.get("groups")
    group_ids = None
    kinds_by_id = {}  # id группы -> kind (v0.4); пусто — легаси (все resources)
    if groups_raw is not None:
        if not isinstance(groups_raw, list) or not groups_raw:
            errors.append("Секция groups пуста или не список")
            groups_raw = None
        else:
            group_ids = set()
            for group in groups_raw:
                if not isinstance(group, dict):
                    errors.append("Элемент groups не объект")
                    continue
                gid = group.get("id")
                if not isinstance(gid, int) or isinstance(gid, bool) or gid <= 0:
                    errors.append(f"Группа {group!r}: id должен быть положительным целым числом")
                    continue
                if gid in group_ids:
                    errors.append(f"Дубликат id группы: {gid}")
                    continue
                group_ids.add(gid)
                emoji = group.get("emoji", "")
                title = group.get("title", "")
                if not isinstance(emoji, str) or not emoji.strip():
                    errors.append(f"Группа {gid}: пустой emoji")
                if not isinstance(title, str) or not title.strip():
                    errors.append(f"Группа {gid}: пустое название")
                # kind (v0.4): отсутствие ключа — resources (легаси).
                kind = group.get("kind", "resources")
                if kind not in GROUP_KINDS:
                    errors.append(
                        f"Группа {gid}: неизвестный kind {kind!r} "
                        "(ожидаются resources/tools/ships/ammunition)"
                    )
                    kind = "resources"
                kinds_by_id[gid] = kind

    def kind_of(item):
        """Семантика группы предмета: для легаси-файлов — resources."""
        if not group_ids:
            return "resources"
        return kinds_by_id.get(item.get("group", 1), "resources")

    items = data.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items отсутствует, не список или пуст")
        return errors, warnings

    # --- Проход 1: имена и собственные поля предметов ----------------
    names = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append("Элемент items не объект")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("Пустое название предмета")
            continue
        if name in names:
            errors.append(f"Дубликат предмета: «{name}»")
        names.add(name)
        weight = item.get("weight", 0)
        if not is_num(weight) or weight < 0:
            errors.append(f"«{name}»: отрицательный или нечисловой вес ({weight!r})")
        if item.get("isCraft") and item.get("recipe") is None:
            errors.append(f"«{name}»: источник «Крафт» без рецепта")
        if item.get("recipe") is not None and not item.get("isCraft"):
            warnings.append(f"«{name}»: есть рецепт, но флаг isCraft выключен")
        group = item.get("group", 1)
        if group_ids is not None:
            # Секция groups есть: группа предмета обязана существовать.
            if group not in group_ids:
                errors.append(f"«{name}»: группа {group!r} отсутствует в секции groups")
        elif group not in KNOWN_GROUPS:
            # Легаси-файл без groups: работает встроенный список 1/2/3.
            warnings.append(
                f"«{name}»: неизвестная группа {group!r} (приложение покажет как «Ресурсы»)"
            )

        # Только крафт (v0.4/v0.5): орудия/корабли/боеприпасы — без
        # мануфактуры и чужих источников; у юнитов (орудия/корабли)
        # ещё и без веса (крафт-время/выход — в проходе 2, вместе
        # с рецептами). У боеприпасов вес и время ЗАДАЮТСЯ (вес партии
        # нужен смете калькулятора).
        kind = kind_of(item)
        if kind in CRAFT_ONLY_KINDS:
            title_kind = CRAFT_ONLY_TITLES[kind]
            if not item.get("isCraft") or item.get("recipe") is None:
                errors.append(f"«{name}»: предмет группы {title_kind} создаётся только крафтом")
            if item.get("isSea") or item.get("isManufactory") or item.get("isMarket") or item.get("isCurrency"):
                errors.append(f"«{name}»: у {title_kind} источник только «Крафт»")
            if kind in UNIT_KINDS and weight != 0:
                errors.append(f"«{name}»: у {title_kind} нет веса")
            if item.get("manufactory") is not None:
                errors.append(f"«{name}»: у {title_kind} нет мануфактуры")

    # --- Проход 2: ссылки, рецепты, мануфактуры ----------------------
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue

        recipe = item.get("recipe")
        if recipe is not None:
            output = recipe.get("outputQuantity")
            if not is_num(output) or output <= 0:
                errors.append(f"«{name}»: выход рецепта должен быть больше 0")
            if OLD_TIME_KEY in recipe:
                errors.append(
                    f"«{name}»: устаревший ключ {OLD_TIME_KEY} (формат 1) — "
                    "переименуй в craftTime"
                )
            craft_time = recipe.get("craftTime")
            if craft_time is None:
                errors.append(
                    f"«{name}»: в рецепте нет craftTime — крафт-время в единицах (не в часах)"
                )
            elif not is_num(craft_time) or craft_time < 0:
                errors.append(f"«{name}»: крафт-время должно быть числом ≥ 0")
            materials = recipe.get("materials", [])
            if not isinstance(materials, list):
                errors.append(f"«{name}»: materials не список")
            else:
                for material in materials:
                    mname = material.get("name")
                    if mname not in names:
                        errors.append(f"«{name}»: материал «{mname}» отсутствует в каталоге")
                    qty = material.get("quantity")
                    if not is_num(qty) or qty <= 0:
                        errors.append(
                            f"«{name}»: количество материала «{mname}» должно быть больше 0"
                        )

            # Только крафт (v0.4/v0.5): у юнитов (орудия/корабли) нет
            # крафт-времени, у кораблей выход всегда 1; материалы всех
            # крафт-only групп — только с флагом материала.
            kind = kind_of(item)
            if kind in CRAFT_ONLY_KINDS and recipe is not None:
                if kind in UNIT_KINDS:
                    craft_time = recipe.get("craftTime")
                    if is_num(craft_time) and craft_time != 0:
                        errors.append(f"«{name}»: у {CRAFT_ONLY_TITLES[kind]} нет крафт-времени")
                    output = recipe.get("outputQuantity")
                    if kind == "ships" and is_num(output) and output != 1:
                        errors.append(f"«{name}»: выход корабля всегда 1 шт.")
                items_by_name = {
                    i.get("name"): i
                    for i in items
                    if isinstance(i, dict) and isinstance(i.get("name"), str)
                }
                for material in recipe.get("materials", []):
                    mitem = items_by_name.get(material.get("name"))
                    if mitem is not None and not mitem.get(MATERIAL_FLAG[kind], False):
                        errors.append(
                            f"«{name}»: материал «{material.get('name')}» "
                            f"без флага {FLAG_LABEL[kind]}"
                        )

        manufactory = item.get("manufactory")
        if manufactory is not None:
            for resource in manufactory.get("consumables", []):
                if resource not in names:
                    errors.append(
                        f"«{name}»: ресурс мануфактуры «{resource}» отсутствует в каталоге"
                    )
            levels = manufactory.get("levels", [])
            if levels:  # либо пусто (свободный ввод), либо ровно LEVEL_COUNT
                if len(levels) != LEVEL_COUNT:
                    errors.append(
                        f"«{name}»: уровней мануфактуры {len(levels)}, ожидается {LEVEL_COUNT}"
                    )
                for index, level in enumerate(levels, start=1):
                    speed = level.get("speedPerHour")
                    if not is_num(speed) or speed <= 0:
                        errors.append(
                            f"«{name}»: скорость уровня {index} должна быть больше 0"
                        )
                    for resource, qty in level.get("consumesPerHour", {}).items():
                        if not is_num(qty) or qty < 0:
                            errors.append(
                                f"«{name}»: расход «{resource}» на уровне {index} "
                                "не может быть отрицательным"
                            )
                        if resource not in names:
                            errors.append(
                                f"«{name}»: ресурс уровня {index} «{resource}» "
                                "отсутствует в каталоге"
                            )
            if not isinstance(manufactory.get("availableInDangerousWaters"), bool):
                errors.append(
                    f"«{name}»: у мануфактуры нет булева флага availableInDangerousWaters"
                )
            if manufactory.get("canUsePrisoners") and PRISONERS_ITEM not in names:
                warnings.append(
                    f"«{name}»: canUsePrisoners, но предмета «{PRISONERS_ITEM}» нет в каталоге"
                )

    # --- Проход 3: циклы в графе рецептов (порт CalcSession) ---------
    # Ребро: предмет с рецептом -> материал, у которого тоже есть рецепт.
    # У материала без рецепта ветка заканчивается (он терминальный).
    recipe_of = {
        item["name"]: item["recipe"]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("recipe"), dict)
    }
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(recipe_of, WHITE)

    def dfs(node, path):
        color[node] = GRAY
        path.append(node)
        for material in recipe_of[node].get("materials", []):
            nxt = material.get("name")
            if nxt not in recipe_of:
                continue
            if color[nxt] == GRAY:
                cycle = path[path.index(nxt):] + [nxt]
                errors.append(
                    "Обнаружен цикл в рецептах: "
                    + " → ".join(cycle)
                    + ". Проверь рецепты предметов или выбери для одного из них другой источник."
                )
            elif color[nxt] == WHITE:
                dfs(nxt, path)
        path.pop()
        color[node] = BLACK

    sys.setrecursionlimit(10000)
    for start in recipe_of:
        if color[start] == WHITE:
            dfs(start, [])

    return errors, warnings


def validate_renames(path, catalog_names):
    """Журнал переименований (renames.json): append-only, применяется
    устройствами по порядку при «Обновить БД» для миграции юзер-данных
    (цены/дефолты/мануфактуры/планы) на новые имена."""
    errors, warnings = [], []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"renames.json: не удалось прочитать/разобрать JSON: {exc}"], []

    if data.get("app") != APP_TAG:
        errors.append(f"renames.json: чужая метка приложения (app = «{data.get('app')}»)")
    renames = data.get("renames")
    if not isinstance(renames, list):
        errors.append("renames.json: renames не список")
        return errors, warnings

    for index, entry in enumerate(renames, start=1):
        if not isinstance(entry, dict):
            errors.append(f"renames.json: запись {index} не объект")
            continue
        old = entry.get("from")
        new = entry.get("to")
        if (
            not isinstance(old, str) or not old.strip()
            or not isinstance(new, str) or not new.strip()
        ):
            errors.append(f"renames.json: запись {index}: пустые from/to")
            continue
        if old == new:
            errors.append(f"renames.json: запись {index}: переименование в то же имя («{old}»)")
        at = entry.get("at", 0)
        if not is_num(at) or at < 0:
            errors.append(f"renames.json: запись {index}: некорректная метка времени at")
        if new not in catalog_names:
            warnings.append(
                f"renames.json: запись {index}: «{old}» → «{new}», но предмета «{new}» "
                "нет в текущем каталоге (возможно, позже переименован снова)"
            )
    return errors, warnings


def validate_texts(path):
    """Структурная проверка texts.json (зеркало TextsFile.validate).

    Ошибки: чужая метка app, неверная версия формата, непустые ключи и
    значения. Незнакомые идентификаторы НЕ проверяются — реестр блоков
    живёт в приложении, файл хранит только отличия (прямая
    совместимость версий).
    """
    errors, warnings = [], []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path}: не удалось прочитать/разобрать JSON: {exc}"], warnings

    if not isinstance(data, dict):
        return [f"{path}: ожидается JSON-объект"], warnings

    app = data.get("app")
    if app != TEXTS_APP_TAG:
        errors.append(f'{path}: метка app=" {app}" не "{TEXTS_APP_TAG}" — это не файл текстов WoSB')

    version = data.get("formatVersion")
    if version != TEXTS_FORMAT_VERSION:
        errors.append(
            f"{path}: formatVersion={version}, ожидается {TEXTS_FORMAT_VERSION} "
            "(файл новой/устаревшей версии не примет текущее приложение)"
        )

    texts = data.get("texts")
    if not isinstance(texts, dict):
        errors.append(f"{path}: секция texts отсутствует или не объект")
        return errors, warnings

    for key, value in texts.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{path}: пустой или нестроковый идентификатор блока")
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f'{path}: блок «{key}» — пустой или нестроковый текст')
    return errors, warnings


def main():
    if len(sys.argv) not in (2, 3, 4):
        print(
            "Использование: validate_catalog.py <catalog.json> [renames.json] [texts.json]",
            file=sys.stderr,
        )
        return 2

    try:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ОШИБКА: не удалось прочитать/разобрать JSON: {exc}")
        return 1

    errors, warnings = validate(data)

    if len(sys.argv) == 3:
        items = data.get("items") or []
        catalog_names = {
            i.get("name") for i in items if isinstance(i, dict) and isinstance(i.get("name"), str)
        }
        r_errors, r_warnings = validate_renames(sys.argv[2], catalog_names)
        errors.extend(r_errors)
        warnings.extend(r_warnings)

    if len(sys.argv) == 4:
        t_errors, t_warnings = validate_texts(sys.argv[3])
        errors.extend(t_errors)
        warnings.extend(t_warnings)

    items = data.get("items") or []
    n_craft = sum(1 for i in items if isinstance(i, dict) and i.get("isCraft"))
    n_manuf = sum(1 for i in items if isinstance(i, dict) and i.get("manufactory") is not None)
    n_groups = len(data.get("groups") or [])
    groups_note = f", групп: {n_groups}" if n_groups else " (легаси без groups)"
    texts_note = ""
    if len(sys.argv) == 4:
        with open(sys.argv[3], encoding="utf-8") as fh:
            t_data = json.load(fh)
        n_texts = len(t_data.get("texts") or {})
        texts_note = f"; тексты: {n_texts} переопределений"
    print(f"Каталог: {len(items)} предметов, {n_craft} с крафтом, {n_manuf} мануфактур{groups_note}{texts_note}")
    for warning in warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")
    for error in errors:
        print(f"ОШИБКА: {error}")
    if errors:
        print(f"Итог: НЕ ПРОЙДЕНО ({len(errors)} ошибок, {len(warnings)} предупреждений)")
        return 1
    print(f"Итог: OK ({len(warnings)} предупреждений)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
