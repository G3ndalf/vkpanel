#!/usr/bin/env python3
"""Добавить проекты в data.json."""
import json
import os

DATA_FILE = os.getenv("DATA_FILE", "/opt/vkpanel/data.json")

# Проекты VK Cloud
PROJECTS = [
    {"name": "cads1", "project_id": "9e03dd35c4dd4d3d86c70a96732ec869", "username": "andreuk03@mail.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "haxo1", "project_id": "a3ebf8ec0ec949deaa5accf7ebc419d1", "username": "aka1234@internet.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "keksik1", "project_id": "72e7b7d9e3fa43a1bf56d41de83cfd3a", "username": "andreuki@bk.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs0349820688", "project_id": "57f8ba0b02f544978beeae505c6fe08d", "username": "andreuki@bk.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs2218407103", "project_id": "0fa4b7ef3e9e4c669361a8e5826b2bf0", "username": "afashagovzzz@outlook.com", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs3039104228", "project_id": "d15e6435d4614ea680100ae2265a94d6", "username": "andreuk03@bk.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs4607965695", "project_id": "5360501de01744989307e8275152d016", "username": "andreuk03@mail.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs4902938951", "project_id": "0920d639479c4988bdb8553d7a6531c9", "username": "andreuki@inbox.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs5528475115", "project_id": "5a6729488ac04faaba3470a3a7bfbc9a", "username": "as.hax@yandex.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "mcs5645848507", "project_id": "ba2a3db8db5c4247b44460fe391e7912", "username": "aka1234@internet.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "set1", "project_id": "0ef7702a7f6f47ffa2d2fb3e01e940e9", "username": "andreuki@inbox.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "soln1", "project_id": "1b916863c53543128c26401e63009f6b", "username": "as.hax@yandex.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "xax1", "project_id": "08e0e2b087544c27893bf460d4e83fc4", "username": "andreuk03@bk.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "xlmmamafree", "project_id": "c88b8fdd590140ce9836bb4d784a2448", "username": "afashagovzzz@outlook.com", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
    {"name": "xxc1", "project_id": "a24e12b032c54d41b2dc45023dc7d9dd", "username": "sadscz123@mail.ru", "auth_url": "https://infra.mail.ru:35357/v3/", "password": "Haxoastemir"},
]

def main():
    # Загружаем существующие данные
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    
    # Добавляем проекты
    data["projects"] = PROJECTS
    
    # Сохраняем
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"Added {len(PROJECTS)} projects to {DATA_FILE}")

if __name__ == "__main__":
    main()
