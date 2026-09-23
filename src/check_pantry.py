from datetime import datetime
from database import get_connection
from engine import PantryDepletionEngine

def check_current_pantry(current_date_str: str):
    engine = PantryDepletionEngine()
    current_date = datetime.strptime(current_date_str, "%Y-%m-%d")
    
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT item_name, category, purchase_date, user_lambda FROM inventory")
    items = cursor.fetchall()
    conn.close()

    print(f"\n--- Pantry Status as of {current_date_str} ---")
    for item in items:
        p_date = datetime.strptime(item["purchase_date"], "%Y-%m-%d")
        days_elapsed = (current_date - p_date).days
        
        prob_depleted = engine.compute_depletion_probability(
            category=item["category"],
            days_elapsed=days_elapsed,
            user_lambda=item["user_lambda"]
        )
        
        status = "⚠️ RESTOCK NEEDED" if prob_depleted >= 0.70 else "✅ IN STOCK"
        print(f"[{status}] {item['item_name']} ({item['category']}):")
        print(f"   Age: {days_elapsed} days | Personal Lambda: {item['user_lambda']:.1f}d | P(Empty): {prob_depleted * 100:.1f}%\n")

if __name__ == "__main__":
    # Check pantry state on 2026-08-20 (10 days after Trip 2, 19 days after Trip 1)
    check_current_pantry("2026-08-20")