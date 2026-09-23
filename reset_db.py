import sqlite3
import os

def wipe_database():
    db_path = os.path.join('data', 'pantry.db')
    if not os.path.exists(db_path):
        print("Database not found at data/pantry.db")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # Flush the tainted intervals and current inventory
    c.execute("DELETE FROM purchase_history")
    c.execute("DELETE FROM inventory")
    
    conn.commit()
    conn.close()
    print("Database wiped successfully. Ready for chronological ingestion.")

if __name__ == "__main__":
    wipe_database()