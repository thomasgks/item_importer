# Style Variant Images - one row per size/item code of each Style Attribute
# Variant (style + colour), with up to 4 website images of that colour.
# Needs the "Style Attribute Variant" doctype (child tables: Color Size Item,
# Website Slideshow Item) to exist on the site.

import frappe
from frappe import _
from frappe.utils import get_url


def execute(filters=None):
    filters = frappe._dict(filters or {})
    return get_columns(), get_data(filters)


def get_columns():
    return [
        {"fieldname": "brand", "label": _("Brand"), "fieldtype": "Link", "options": "Brand", "width": 130},
        {"fieldname": "season", "label": _("Season"), "fieldtype": "Data", "width": 80},
        {"fieldname": "style", "label": _("Style"), "fieldtype": "Data", "width": 100},
        {"fieldname": "attribute_value", "label": _("Attribute Value"), "fieldtype": "Data", "width": 110},
        {"fieldname": "attribute_name", "label": _("Attribute Name"), "fieldtype": "Data", "width": 130},
        {"fieldname": "item_code", "label": _("Item Code"), "fieldtype": "Link", "options": "Item", "width": 110},
        {"fieldname": "size", "label": _("Size"), "fieldtype": "Data", "width": 70},
        {"fieldname": "image1", "label": _("Image1"), "fieldtype": "Data", "width": 280},
        {"fieldname": "image2", "label": _("Image2"), "fieldtype": "Data", "width": 280},
        {"fieldname": "image3", "label": _("Image3"), "fieldtype": "Data", "width": 280},
        {"fieldname": "image4", "label": _("Image4"), "fieldtype": "Data", "width": 280},
    ]


def image_sql(offset):
    return f"""(SELECT si.image FROM `tabWebsite Slideshow Item` si
                WHERE si.parent = sav.name AND si.parentfield = 'images'
                ORDER BY si.idx LIMIT 1 OFFSET {int(offset)})"""


def get_data(filters):
    conditions = []
    values = {}

    brands = filters.get("brand")
    if brands:
        if isinstance(brands, str):
            brands = [brands]
        conditions.append("i.brand IN %(brands)s")
        values["brands"] = tuple(brands)

    seasons = filters.get("season")
    if seasons:
        if isinstance(seasons, str):
            seasons = [seasons]
        conditions.append("va_season.attribute_value IN %(seasons)s")
        values["seasons"] = tuple(seasons)

    if filters.get("style"):
        conditions.append("sav.item_style LIKE %(style)s")
        values["style"] = f"%{filters.style.strip()}%"
    if filters.get("item_code"):
        conditions.append("csi.item_code LIKE %(item_code)s")
        values["item_code"] = f"%{filters.item_code.strip()}%"
    if filters.get("attribute_value"):
        conditions.append("(sav.attribute_value LIKE %(attr)s OR sav.attribute_name LIKE %(attr)s)")
        values["attr"] = f"%{filters.attribute_value.strip()}%"
    if filters.get("item_group"):
        conditions.append("sav.item_group = %(item_group)s")
        values["item_group"] = filters.item_group
    if filters.get("published") == "Yes":
        conditions.append("sav.is_published = 1")
    elif filters.get("published") == "No":
        conditions.append("sav.is_published = 0")
    if filters.get("missing_images"):
        conditions.append(
            "(SELECT COUNT(*) FROM `tabWebsite Slideshow Item` mi "
            "WHERE mi.parent = sav.name AND mi.parentfield = 'images') = 0"
        )

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    query = f"""
        SELECT
            i.brand AS brand,
            va_season.attribute_value AS season,
            sav.item_style AS style,
            sav.attribute_value AS attribute_value,
            sav.attribute_name AS attribute_name,
            csi.item_code AS item_code,
            csi.size AS size,
            {image_sql(0)} AS image1,
            {image_sql(1)} AS image2,
            {image_sql(2)} AS image3,
            {image_sql(3)} AS image4
        FROM `tabStyle Attribute Variant` sav
        INNER JOIN `tabColor Size Item` csi
                ON csi.parent = sav.name AND csi.parentfield = 'sizes'
        LEFT JOIN `tabItem` i ON i.name = csi.item_code
        LEFT JOIN `tabItem Variant Attribute` va_season
               ON va_season.parent = csi.item_code AND va_season.attribute = 'Season'
        {where}
        ORDER BY i.brand, sav.item_style, sav.attribute_value, csi.idx
    """
    data = frappe.db.sql(query, values, as_dict=True)

    # Turn /files/xxx.jpg into the full URL, e.g. https://shoearena.sa/files/xxx.jpg
    for row in data:
        for key in ("image1", "image2", "image3", "image4"):
            path = row.get(key)
            if path and not path.startswith(("http://", "https://")):
                row[key] = get_url(path)
    return data


@frappe.whitelist()
def get_seasons(txt=None):
    rows = frappe.db.sql(
        """SELECT attribute_value FROM `tabItem Attribute Value`
           WHERE parent = 'Season' AND attribute_value LIKE %(txt)s
           ORDER BY attribute_value""",
        {"txt": f"%{txt or ''}%"},
    )
    return [{"value": r[0], "description": ""} for r in rows]
