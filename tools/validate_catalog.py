#!/usr/bin/env python3
"""CI-валидатор каталога WoSB (catalog.json).

Повторяет правила парсера приложения (CatalogParser, Kotlin,
app/src/main/java/.../data/catalog/CatalogFile.kt) и добавляет проверку,
которая в приложении выполняется во время расчёта: обнаружение циклов
в графе рецептов — порт DFS из CalcSession (тот же формат сообщения
«Обнаружен цикл в рецептах: A → B → A»).

Выход: 0 — ошибок нет (предупреждения допустимы), 1 — есть ошибки.
Использование: python3 tools/validate_catalog.py catalog.json
"""

import json
import sys

# Константы — зеркало CatalogFile.kt / ManufactoryLevels.kt приложения.
APP_TAG = "wosb-catalog"
FORMAT_VERSION = 1
LEVEL_COUNT = 4             # уровней у мануфактуры: либо 0, либо ровно 4
PRISONERS_ITEM = "Пленные"  # служебный предмет-ресурс пленных
KNOWN_GROUPS = {1, 2, 3}    # 1 «📦 Предметы», 2 «💣 Орудия», 3 «⛵ Корабли»


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
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt > FORMAT_VERSION:
        errors.append(
            f"Версия формата {fmt!r} выше поддерживаемой {FORMAT_VERSION} — обновите приложение"
        )

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
        if group not in KNOWN_GROUPS:
            warnings.append(
                f"«{name}»: неизвестная группа {group!r} (приложение покажет как «Предметы»)"
            )

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
            craft_time = recipe.get("craftTimeHours")
            if not is_num(craft_time) or craft_time < 0:
                errors.append(f"«{name}»: отрицательное время крафта")
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


def main():
    if len(sys.argv) != 2:
        print("Использование: validate_catalog.py <catalog.json>", file=sys.stderr)
        return 2

    try:
        with open(sys.argv[1], encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ОШИБКА: не удалось прочитать/разобрать JSON: {exc}")
        return 1

    errors, warnings = validate(data)

    items = data.get("items") or []
    n_craft = sum(1 for i in items if isinstance(i, dict) and i.get("isCraft"))
    n_manuf = sum(1 for i in items if isinstance(i, dict) and i.get("manufactory") is not None)
    print(f"Каталог: {len(items)} предметов, {n_craft} с крафтом, {n_manuf} мануфактур")
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
