import frappe
import pandas as pd

def test():
    print("TEST OK")
    
def rename_template_and_variants(file_path):
    # Load CSV or Excel
    if file_path.endswith(".csv"):
        df = pd.read_csv(file_path,dtype=str)
    else:
        df = pd.read_excel(file_path,dtype=str)

    for _, row in df.iterrows():
        old_id = row["old_id"]
        new_id = row["new_id"]

        print(f"\nProcessing: {old_id} → {new_id}")

        # 1. Check if item exists
        if not frappe.db.exists("Item", old_id):
            print(f"❌ Item {old_id} does not exist. Skipping.")
            continue

        # 2. Check if item is a template (has_variants = 1)
        is_template = frappe.db.get_value("Item", old_id, "has_variants")

        if not is_template:
            print(f"❌ {old_id} is NOT a template item (has_variants=0). Skipping.")
            continue

        print(f"✔ {old_id} is a valid template. Proceeding with rename.")

        # 3. Rename the template item
        frappe.rename_doc("Item", old_id, new_id, force=True)
        frappe.db.commit()
        print(f"✔ Renamed Template: {old_id} → {new_id}")

        # 4. Update variant_of for all variants (variant_of is NOT a Link field)
        variants = frappe.get_all("Item", filters={"variant_of": old_id}, pluck="name")

        print(f"Found {len(variants)} variants to update.")

        for variant in variants:
            frappe.db.set_value("Item", variant, "variant_of", new_id)
            print(f"  - Updated variant_of: {variant}")

        # 5. Update Item Variant Attribute table (parent is NOT a Link field)
        frappe.db.sql("""
            UPDATE `tabItem Variant Attribute`
            SET parent = %s
            WHERE parent = %s
        """, (new_id, old_id))

        frappe.db.commit()
        print(f"✔ Completed updates for template {new_id}")

    print("\n🎉 All updates completed successfully.")


def update_variant_template_from_csv(file_path):
    # Load CSV or Excel
    if file_path.endswith(".csv"):
        df = pd.read_csv(file_path, dtype=str)
    else:
        df = pd.read_excel(file_path, dtype=str)

    for _, row in df.iterrows():
        item_id = row["item_id"]
        template_id = row["template_id"]

        print(f"\nProcessing item {item_id} → template {template_id}")

        # 1. Check if item exists
        if not frappe.db.exists("Item", item_id):
            print(f"❌ Item {item_id} does not exist. Skipping.")
            continue

        # 2. Check if template exists AND is a template
        is_template = frappe.db.get_value(
            "Item",
            {"item_code": ["=", template_id]},
            "has_variants"
        )

        if not is_template:
            print(f"❌ {template_id} is NOT a valid template (has_variants=0 or missing). Skipping.")
            continue

        # 3. Update variant_of
        frappe.db.set_value("Item", item_id, "variant_of", template_id)
        print(f"✔ Updated variant_of: {item_id} → {template_id}")

        # 4. Update Item Variant Attribute parent
        frappe.db.sql("""
            UPDATE `tabItem Variant Attribute`
            SET variant_of = %s
            WHERE parent = %s
        """, (template_id, item_id))

        frappe.db.commit()
        print(f"✔ Updated Item Variant Attribute parent for {item_id}")

    print("\n🎉 All updates completed successfully.")