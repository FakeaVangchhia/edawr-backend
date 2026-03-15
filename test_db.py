import psycopg

try:
    conn = psycopg.connect("postgresql://postgres:lehkha@localhost:5432/airo")
    print("Connection to 'airo' successful!")
    conn.close()
except psycopg.OperationalError as e:
    print(f"OperationalError connecting to 'airo': {e}")
    if "database \"airo\" does not exist" in str(e):
        try:
            conn_pg = psycopg.connect("postgresql://postgres:lehkha@localhost:5432/postgres")
            conn_pg.autocommit = True
            with conn_pg.cursor() as cur:
                cur.execute("CREATE DATABASE airo;")
            print("Successfully created database 'airo'.")
            conn_pg.close()
        except Exception as e2:
            print(f"Error connecting to 'postgres' to create DB: {e2}")
    else:
        print(f"Other operational error: {e}")
except Exception as e:
    print(f"Error connecting to 'airo': {e}")
