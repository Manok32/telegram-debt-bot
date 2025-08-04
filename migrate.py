# --- ФАЙЛ: migrate.py (ИСПРАВЛЕННАЯ ВЕРСИЯ) ---
import os
import csv
import psycopg2

# --- НАЧАЛО ИСПРАВЛЕНИЯ ---
# Определяем, где находится наш скрипт, чтобы правильно найти CSV файлы
# __file__ - это переменная, содержащая путь к текущему файлу
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_CSV_PATH = os.path.join(BASE_DIR, 'users.csv')
TRANSACTIONS_CSV_PATH = os.path.join(BASE_DIR, 'transactions.csv')
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("!!! ОШИБКА: URL базы данных не найден. Миграция невозможна.")
else:
    print("Подключение к базе данных PostgreSQL...")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    print("Подключение успешно.")

    try:
        # Очищаем таблицы перед вставкой
        print("Очистка старых данных...")
        cur.execute("DELETE FROM transactions;")
        cur.execute("DELETE FROM users;")
        print("Таблицы очищены.")

        # Загружаем пользователей
        print(f"Загрузка данных из {USERS_CSV_PATH}...")
        with open(USERS_CSV_PATH, 'r', encoding='utf-8') as f: # <-- ИСПРАВЛЕНО
            reader = csv.reader(f)
            next(reader) # Пропускаем заголовок
            for row in reader:
                cur.execute(
                    "INSERT INTO users (user_id, chat_id, first_name, username) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (row[0], row[1], row[2], row[3])
                )
        print("Пользователи успешно загружены.")

        # Загружаем транзакции
        print(f"Загрузка данных из {TRANSACTIONS_CSV_PATH}...")
        with open(TRANSACTIONS_CSV_PATH, 'r', encoding='utf-8') as f: # <-- ИСПРАВЛЕНО
            reader = csv.reader(f)
            next(reader) # Пропускаем заголовок
            for row in reader:
                cur.execute(
                    "INSERT INTO transactions (chat_id, creditor_id, debtor_id, amount, comment, timestamp) VALUES (%s, %s, %s, %s, %s, %s)",
                    (row[1], row[2], row[3], row[4], row[5], row[6])
                )
        print("Транзакции успешно загружены.")

        conn.commit()
        print("\n✅ МИГРАЦИЯ ДАННЫХ УСПЕШНО ЗАВЕРШЕНА!")

    except FileNotFoundError as e:
        print(f"\n❌ ОШИБКА: Файл не найден! Убедитесь, что users.csv и transactions.csv загружены на GitHub. Детали: {e}")
        conn.rollback()
    except Exception as e:
        print(f"\n❌ ПРОИЗОШЛА ОШИБКА: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()
        print("Соединение с базой данных закрыто.")
