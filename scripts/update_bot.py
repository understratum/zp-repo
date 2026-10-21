import os
import re
import urllib.request
import json
import subprocess
def read_zon(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()

def write_zon(filepath, content):
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

def extract_field(content, field_name):
    match = re.search(rf'\.{field_name}\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else None

def update_field(content, field_name, new_value):
    return re.sub(rf'(\.{field_name}\s*=\s*")([^"]+)(")', rf'\g<1>{new_value}\g<3>', content)

def get_latest_github_release(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={'User-Agent': 'zp-update-bot'})
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            return data['tag_name'].lstrip('v') # Убираем 'v' если есть (например, "v1.0.0" -> "1.0.0")
    except Exception as e:
        print(f"Не удалось получить релиз для {owner}/{repo}: {e}")
        return None

def main():
    print("Запуск бота обновления пакетов zp...")

    for item in os.listdir('.'):
        if os.path.isdir(item) and not item.startswith('.'):
            zon_file = f"{item}/{item}.zon"
            
            if os.path.exists(zon_file):
                print(f"\nПроверка пакета: {item}")
                content = read_zon(zon_file)
                
                current_version = extract_field(content, 'version')
                url = extract_field(content, 'url')
                
                if not current_version or not url:
                    print(f"Пропуск: не найдены поля .version или .url")
                    continue
            
                github_match = re.search(r'github\.com/([^/]+)/([^/]+)', url)
                if not github_match:
                    print(f"Пропуск: URL не является ссылкой на GitHub")
                    continue
                
                owner, repo_with_ext = github_match.groups()
                repo = repo_with_ext.replace('.git', '')
          
                latest_version = get_latest_github_release(owner, repo)
                
                if not latest_version:
                    continue
                
                if latest_version != current_version:
                    print(f"Найдено обновление: {current_version} -> {latest_version}")
                    new_content = update_field(content, 'version', latest_verssion
                    if current_version in url:
                        new_url = url.replace(current_version, latest_version)
                        new_content = update_field(new_content, 'url', latest_version) # Обновляем и саму строку URL
                        
                    write_zon(zon_file, new_content)
                    print(f"Файл {zon_file} обновлен.")
                else:
                    print(f"Версия актуальна ({current_version})")

if __name__ == "__main__":
    main()
