"""The facts the search rules compare against: store names and categories.

Try it:
    uv run python -m analysis.code_evals.catalog
"""

import sqlite3
from pathlib import Path

# The shop database: <repo>/data/cartwheel.db
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "cartwheel.db"


def normalise(text):
    """Return text in a plain form that is easy to compare.

    "home_and_kitchen" and "Home/Kitchen" both become "home kitchen".
    """
    text = text.lower()

    # These characters count as a space.
    for character in "_/-&,.!?'\"":
        text = text.replace(character, " ")

    # "and" carries no meaning here. split() also removes extra spaces.
    kept_words = []
    for word in text.split():
        if word != "and":
            kept_words.append(word)

    return " ".join(kept_words)


class Catalog:
    """Store names and product categories, read once from the database."""

    def __init__(self, db_path=DB_PATH):
        # sqlite3.connect creates an empty database when the file is missing.
        # Every rule would then find nothing, so stop with an error instead.
        if not Path(db_path).exists():
            raise FileNotFoundError(f"database not found: {db_path}")

        connection = sqlite3.connect(db_path)

        self.store_names = []
        for row in connection.execute("SELECT name FROM stores"):
            self.store_names.append(normalise(row[0]))

        self.categories = []
        for row in connection.execute("SELECT category FROM products UNION SELECT category FROM stores"):
            self.categories.append(normalise(row[0]))

        connection.close()

    def find_store_name_in(self, text):
        """Return the store name found inside text, or None if there is none.

        The spaces make it match whole words only: "petal stem" is found in
        "petal stem vase", but not in "petal stemware".
        """
        text_with_spaces = " " + normalise(text) + " "

        for store_name in self.store_names:
            if " " + store_name + " " in text_with_spaces:
                return store_name

        return None

    def is_category(self, text):
        """Return True when text is exactly one category, singular or plura(ie 's' &
        'ies')"""
        word = normalise(text)

        if word in self.categories:
            return True
        if word + "s" in self.categories:
            return True
        if word.endswith("s") and word[:-1] in self.categories:
            return True
        if word.endswith("ies") and word[:-3] + "y" in self.categories:
            return True

        return False


# This block runs only when you run this file directly.
if __name__ == "__main__":
    catalog = Catalog()
    print(len(catalog.store_names), "stores:", catalog.store_names)
    print(len(catalog.categories), "categories:", catalog.categories)
    print()
    print("Store in 'Atlas Stationery':", catalog.find_store_name_in("Atlas Stationery"))
    print("Store in 'blue ceramic mug':", catalog.find_store_name_in("blue ceramic mug"))
    print("Is 'home kitchen' a category?", catalog.is_category("home kitchen"))
    print("Is 'gardening' a category?", catalog.is_category("gardening"))
