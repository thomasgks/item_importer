# Item Import List - lists Items with the same fields used by the Item Importer
# Excel template (Items.xlsx), one column per importer field, same order.

import frappe
from frappe import _


def execute(filters=None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns():
    # (fieldname, label [= Excel row-2 description], fieldtype, options, width)
    cols = [
        ("brand", "BRAND", "Link", "Brand", 130),
        ("group_level1", "DEPARTMENT", "Data", None, 120),
        ("group_level2", "CLASS", "Data", None, 120),
        ("group_level3", "SUBCLASS", "Data", None, 120),
        ("item_code", "ITEM NO", "Link", "Item", 120),
        ("variant_of", "STYEL NO", "Link", "Item", 110),
        ("custom_style_code", "STYEL CODE", "Data", None, 110),
        ("item_name", "ITEM NAME", "Data", None, 200),
        ("custom_item_name_ar", "ITEM NAME ARABIC", "Data", None, 160),
        ("description", "DESCRIPTION", "Data", None, 200),
        ("color", "Color", "Data", None, 100),
        ("color_code", "Color Name", "Data", None, 100),
        ("size", "Size", "Data", None, 80),
        ("season", "Season", "Data", None, 80),
        ("assortment", "Assortment", "Data", None, 100),
        ("barcode", "UPC", "Data", None, 140),
        ("custom_material", "COMPOSITION/MATERIAL", "Data", None, 140),
        ("custom_image_url", "IMAGE URL", "Data", None, 160),
        ("custom_bin_no", "BIN NO", "Data", None, 90),
        ("custom_dcs", "DCS", "Data", None, 100),
        ("custom_vendor_code", "VENDOR CODE", "Data", None, 100),
        ("custom_vendor_name", "VENDOR NAME", "Data", None, 160),
        ("custom_vendor_currency", "VENDOR CURRENCY", "Data", None, 90),
        ("custom_exchange_rate", "EXCHANGE RATE", "Float", None, 90),
        ("custom_vendor_cost", "VENDOR COST", "Currency", None, 100),
        ("valuation_rate", "COST (SAR)", "Currency", None, 100),
        ("price_level3", "MRP WVAT", "Currency", None, 100),
        ("price_level1", "RSP WVAT", "Currency", None, 100),
        ("price_level2", "WSP WVAT", "Currency", None, 100),
        ("is_stock_item", "Maintain Stock", "Check", None, 90),
        ("disabled", "Disabled (1 or 0)", "Check", None, 90),
        ("is_sales_item", "Allow Sales", "Check", None, 90),
    ]
    columns = []
    for fieldname, label, fieldtype, options, width in cols:
        col = {
            "fieldname": fieldname,
            "label": _(label),
            "fieldtype": fieldtype,
            "width": width,
        }
        if options:
            col["options"] = options
        columns.append(col)
    return columns


def price_sql(price_list):
    return f"""(SELECT ip.price_list_rate FROM `tabItem Price` ip
                WHERE ip.item_code = i.name AND ip.price_list = '{price_list}'
                ORDER BY ip.modified DESC LIMIT 1)"""


def get_data(filters):
    conditions = []
    values = {}

    # ---- Item Group (includes all child groups) ----
    if filters.get("item_group"):
        lft, rgt = frappe.db.get_value("Item Group", filters.item_group, ["lft", "rgt"]) or (0, 0)
        conditions.append(
            "i.item_group IN (SELECT name FROM `tabItem Group` WHERE lft >= %(ig_lft)s AND rgt <= %(ig_rgt)s)"
        )
        values.update(ig_lft=lft, ig_rgt=rgt)

    # ---- Brand (single or multi-select) ----
    brands = filters.get("brand")
    if brands:
        if isinstance(brands, str):
            brands = [brands]
        conditions.append("i.brand IN %(brands)s")
        values["brands"] = tuple(brands)

    # ---- Exact / pattern filters ----
    if filters.get("item_code"):
        conditions.append("i.item_code LIKE %(item_code)s")
        values["item_code"] = f"%{filters.item_code.strip()}%"
    if filters.get("item_name"):
        conditions.append("(i.item_name LIKE %(item_name)s OR i.custom_item_name_ar LIKE %(item_name)s)")
        values["item_name"] = f"%{filters.item_name.strip()}%"
    if filters.get("variant_of"):
        conditions.append("i.variant_of = %(variant_of)s")
        values["variant_of"] = filters.variant_of
    if filters.get("style_code"):
        conditions.append("i.custom_style_code LIKE %(style_code)s")
        values["style_code"] = f"%{filters.style_code.strip()}%"
    if filters.get("barcode"):
        conditions.append(
            "EXISTS (SELECT 1 FROM `tabItem Barcode` fb WHERE fb.parent = i.name AND fb.barcode LIKE %(barcode)s)"
        )
        values["barcode"] = f"%{filters.barcode.strip()}%"
    if filters.get("vendor"):
        conditions.append("(i.custom_vendor_code LIKE %(vendor)s OR i.custom_vendor_name LIKE %(vendor)s)")
        values["vendor"] = f"%{filters.vendor.strip()}%"
    if filters.get("dcs"):
        conditions.append("i.custom_dcs LIKE %(dcs)s")
        values["dcs"] = f"%{filters.dcs.strip()}%"

    # ---- Variant attributes ----
    for key, alias in (("color", "va_color"), ("size", "va_size"), ("season", "va_season"), ("assortment", "va_assort")):
        if filters.get(key):
            conditions.append(f"{alias}.attribute_value LIKE %({key})s")
            values[key] = f"%{filters[key].strip()}%"

    # ---- Item type ----
    item_type = filters.get("item_type")
    if item_type == "Templates":
        conditions.append("i.has_variants = 1")
    elif item_type == "Variants":
        conditions.append("IFNULL(i.variant_of, '') != ''")
    elif item_type == "Standalone":
        conditions.append("i.has_variants = 0 AND IFNULL(i.variant_of, '') = ''")

    # ---- Flags ----
    if filters.get("status") == "Enabled":
        conditions.append("i.disabled = 0")
    elif filters.get("status") == "Disabled":
        conditions.append("i.disabled = 1")
    if filters.get("maintain_stock") == "Yes":
        conditions.append("i.is_stock_item = 1")
    elif filters.get("maintain_stock") == "No":
        conditions.append("i.is_stock_item = 0")
    if filters.get("allow_sales") == "Yes":
        conditions.append("i.is_sales_item = 1")
    elif filters.get("allow_sales") == "No":
        conditions.append("i.is_sales_item = 0")

    # ---- Dates ----
    if filters.get("created_from"):
        conditions.append("DATE(i.creation) >= %(created_from)s")
        values["created_from"] = filters.created_from
    if filters.get("created_to"):
        conditions.append("DATE(i.creation) <= %(created_to)s")
        values["created_to"] = filters.created_to
    if filters.get("modified_from"):
        conditions.append("DATE(i.modified) >= %(modified_from)s")
        values["modified_from"] = filters.modified_from
    if filters.get("modified_to"):
        conditions.append("DATE(i.modified) <= %(modified_to)s")
        values["modified_to"] = filters.modified_to
    if filters.get("synced_from"):
        conditions.append("DATE(i.custom_last_synced) >= %(synced_from)s")
        values["synced_from"] = filters.synced_from
    if filters.get("synced_to"):
        conditions.append("DATE(i.custom_last_synced) <= %(synced_to)s")
        values["synced_to"] = filters.synced_to

    # ---- Data quality ----
    if filters.get("missing_barcode"):
        conditions.append("NOT EXISTS (SELECT 1 FROM `tabItem Barcode` mb WHERE mb.parent = i.name)")
    if filters.get("missing_price"):
        conditions.append(
            "i.has_variants = 0 AND (" + price_sql("PriceLevel1") + " IS NULL OR "
            + price_sql("PriceLevel2") + " IS NULL OR " + price_sql("PriceLevel3") + " IS NULL)"
        )

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
        SELECT
            i.brand AS brand,
            ig1.item_group_name AS group_level1,
            SUBSTRING_INDEX(ig2.item_group_name, '.', -1) AS group_level2,
            SUBSTRING_INDEX(ig3.item_group_name, '.', -1) AS group_level3,
            i.item_code AS item_code,
            i.variant_of AS variant_of,
            i.custom_style_code AS custom_style_code,
            i.item_name AS item_name,
            i.custom_item_name_ar AS custom_item_name_ar,
            i.description AS description,
            va_color.attribute_value AS color,
            va_code.attribute_value AS color_code,
            va_size.attribute_value AS size,
            va_season.attribute_value AS season,
            va_assort.attribute_value AS assortment,
            (SELECT GROUP_CONCAT(b.barcode ORDER BY b.idx SEPARATOR ', ')
               FROM `tabItem Barcode` b WHERE b.parent = i.name) AS barcode,
            i.custom_material AS custom_material,
            i.custom_image_url AS custom_image_url,
            i.custom_bin_no AS custom_bin_no,
            i.custom_dcs AS custom_dcs,
            i.custom_vendor_code AS custom_vendor_code,
            i.custom_vendor_name AS custom_vendor_name,
            i.custom_vendor_currency AS custom_vendor_currency,
            i.custom_exchange_rate AS custom_exchange_rate,
            i.custom_vendor_cost AS custom_vendor_cost,
            i.valuation_rate AS valuation_rate,
            {price_sql("PriceLevel3")} AS price_level3,
            {price_sql("PriceLevel1")} AS price_level1,
            {price_sql("PriceLevel2")} AS price_level2,
            i.is_stock_item AS is_stock_item,
            i.disabled AS disabled,
            i.is_sales_item AS is_sales_item
        FROM `tabItem` i
        LEFT JOIN `tabItem Group` ig3 ON ig3.name = i.item_group
        LEFT JOIN `tabItem Group` ig2 ON ig2.name = ig3.parent_item_group
        LEFT JOIN `tabItem Group` ig1 ON ig1.name = ig2.parent_item_group
        LEFT JOIN `tabItem Variant Attribute` va_color
               ON va_color.parent = i.name AND va_color.attribute = 'Color'
        LEFT JOIN `tabItem Variant Attribute` va_code
               ON va_code.parent = i.name AND va_code.attribute = 'Color Code'
        LEFT JOIN `tabItem Variant Attribute` va_size
               ON va_size.parent = i.name AND va_size.attribute = 'Size'
        LEFT JOIN `tabItem Variant Attribute` va_season
               ON va_season.parent = i.name AND va_season.attribute = 'Season'
        LEFT JOIN `tabItem Variant Attribute` va_assort
               ON va_assort.parent = i.name AND va_assort.attribute = 'Assortment'
        {where}
        ORDER BY i.brand, i.variant_of, i.item_code
    """
    return frappe.db.sql(query, values, as_dict=True)
