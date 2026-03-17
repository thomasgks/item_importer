# item_importer/item_importer/doctype/item_importer/item_importer.py
import frappe
from frappe.model.document import Document
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file
from frappe.utils import now_datetime
import time


class ItemImporter(Document):
    pass


@frappe.whitelist()
def start_import(docname):
    frappe.logger().info("✅ importing started")
    doc = frappe.get_doc("Item Importer", docname)
    if not doc.import_file:
        frappe.throw("Please attach an Excel file before starting import.")

    doc.status = "In Progress"
    doc.progress = 0
    doc.save(ignore_permissions=True)
    frappe.logger().info("✅ Importing job added to enqueue...")
    frappe.enqueue(
        "item_importer.item_importer.doctype.item_importer.item_importer.run_import",
        queue="long",
        timeout=7200,  # 2 hours for large files
        docname=docname,
    )


def run_import(docname):
    start_time = time.time()
    doc = frappe.get_doc("Item Importer", docname)
    frappe.logger().info("✅ Background job started")
    
    # Initialize caches
    cache = {
        'item_groups': {},
        'brands': {},
        'suppliers': {},
        'attributes': {},
        'attribute_values': {},
        'items': {},
        'item_prices': {},
        'barcodes': {},  # Cache for barcode lookups
        'updated_templates': set()  # Track templates already updated in this session
    }
    
    try:
        file_url = doc.import_file
        if not file_url:
            frappe.throw("No file attached.")

        rows = read_xlsx_file_from_attached_file(file_url=file_url) or []
    except Exception:
        doc.status = "Failed"
        doc.save(ignore_permissions=True)
        frappe.logger().error(f"❌ Item Importer File Error")
        frappe.log_error(
            title="Item Importer File Error", message=frappe.get_traceback()
        )
        return

    if len(rows) < 3:
        doc.status = "Failed"
        doc.save(ignore_permissions=True)
        frappe.logger().error(
            f"❌ Excel must have at least 3 rows (fieldnames, descriptions, data)"
        )
        frappe.throw(
            "Excel must have at least 3 rows (fieldnames, descriptions, data)."
        )

    header = rows[0]
    data_rows = rows[2:]  # skip row 1 (fieldnames) and row 2 (descriptions)

    col_index = {str(h).strip(): idx for idx, h in enumerate(header)}

    mandatory_fields = [
        "brand",
        "Group-Level1",
        "Group-Level2",
        "Group-Level3",
        "item_code",
        "item_name",
        "PriceLevel3",
        "PriceLevel1",
        "PriceLevel2",
        "is_stock_item",
        "is_sales_item",
        "disabled",
    ]

    variant_mandatory = [
        "variant_of",
        "Color",
        "Color Code",
        "Size",
        "Season",
        "Assortment",
    ]

    log_doc = frappe.get_doc(
        {
            "doctype": "Item Import Log",
            "item_importer": doc.name,
            "status": "In Progress",
        }
    )
    log_doc.insert(ignore_permissions=True)
    doc.last_log = log_doc.name
    doc.save(ignore_permissions=True)

    total = len(data_rows)
    frappe.logger().info(f"✅ total rows={total}")
    
    # PERFORMANCE: Larger batches for better speed
    processed = 0
    commit_interval = 200
    log_batch_size = 100
    progress_update_interval = 100
    
    success_count = 0
    failure_count = 0
    created_count = 0
    updated_count = 0
    skipped_count = 0
    
    # Batch collections
    entries_batch = []
    item_prices_batch = []

    for row_idx, row in enumerate(data_rows, start=3):
        row_data = _row_to_dict(row, col_index)
        row_data = sanitize_row(row_data)
        row_status = "Success"
        failure_reason = ""
        action_taken = "Created"
        
        # Reduced logging - only log every 100 rows
        if row_idx % 100 == 0:
            frappe.logger().info(
                f"✅ Importing  row={row_idx}, item={row_data.get('item_code')}"
            )

        try:
            _validate_mandatory(row_data, mandatory_fields, row_idx)
            is_variant = bool(row_data.get("variant_of"))
            if is_variant:
                _validate_mandatory(row_data, variant_mandatory, row_idx)

            item_group_name = _ensure_item_group_hierarchy(row_data, cache)
            brand_name = _ensure_brand(row_data, cache)
            if doc.create_purchase_order:
                supplier_name = _ensure_supplier(row_data, cache)
            attributes = _ensure_attributes(row_data, cache)
            
            # Handle item creation or update (includes template updates)
            item_doc, action = _ensure_or_update_item(
                row_data, 
                item_group_name, 
                brand_name, 
                attributes, 
                cache,
                doc.custom_update_existing_items
            )

            is_new_item = (action == "Created")
            is_updated = (action == "Updated")
            is_skipped = (action == "Skipped")
            
            action_taken = action

            # Handle barcodes with duplicate check
            barcode_success, barcode_error = _handle_barcodes(
                item_doc, 
                row_data, 
                doc.custom_update_existing_barcodes,
                is_new_item,
                row_idx,
                cache
            )
            
            if not barcode_success:
                # Barcode duplicate - treat as failure
                frappe.throw(barcode_error)

            # Handle prices based on setting
            if doc.custom_update_existing_prices or is_new_item:
                _collect_item_prices(item_doc, row_data, item_prices_batch, cache, is_update=doc.custom_update_existing_prices)

            # Update counters based on action
            if is_new_item:
                created_count += 1
            elif is_updated:
                updated_count += 1
            elif is_skipped:
                skipped_count += 1
                
            success_count += 1

        except Exception as e:
            row_status = "Failed"
            failure_reason = str(e) if str(e) else frappe.get_traceback()
            failure_count += 1
            frappe.logger().error(f"❌Importer Error Row {row_idx}: {failure_reason[:500]}")

        # Batch log entries with action taken
        entries_batch.append({
            "row_no": row_idx,
            "row_data": frappe.as_json(row_data),
            "status": row_status,
            "failure_reason": failure_reason,
            "action": action_taken if row_status == "Success" else "Failed",
        })

        processed += 1

        # Batch save logs
        if len(entries_batch) >= log_batch_size:
            for entry in entries_batch:
                log_doc.append("entries", entry)
            log_doc.save(ignore_permissions=True)
            entries_batch = []

        # Batch commit
        if processed % commit_interval == 0 or processed == total:
            # Process batched item prices
            if item_prices_batch:
                _process_item_prices_batch(item_prices_batch, doc.custom_update_existing_prices)
                item_prices_batch = []
            
            log_doc.save(ignore_permissions=True)
            frappe.db.commit()

        # Batch progress update
        progress = int(processed * 100 / total)
        if progress != getattr(doc, '_last_progress', 0) and (processed % progress_update_interval == 0 or processed == total):
            doc.progress = progress
            doc.save(ignore_permissions=True)
            doc._last_progress = progress
            
            # Realtime update every 10%
            if progress % 10 == 0:
                frappe.publish_realtime(
                    event="item_import_progress",
                    message={"progress": progress, "docname": doc.name},
                    user=doc.owner,
                )

    # Process remaining batches
    if entries_batch:
        for entry in entries_batch:
            log_doc.append("entries", entry)
        log_doc.save(ignore_permissions=True)
    
    if item_prices_batch:
        _process_item_prices_batch(item_prices_batch, doc.custom_update_existing_prices)

    log_doc.status = "Completed"
    log_doc.success_count = success_count
    log_doc.failure_count = failure_count
    log_doc.save(ignore_permissions=True)

    doc.reload()
    doc.status = "Completed" if failure_count == 0 else "Partially Completed"
    doc.progress = 100
    doc.save(ignore_permissions=True)
    
    # Create Purchase Order if enabled
    frappe.logger().info(f"✅ Creating PO")
    doc.reload()
    if doc.create_purchase_order:
        try:
            _create_purchase_order(log_doc)
        except Exception:
            frappe.log_error(
                title="Item Importer PO Error", message=frappe.get_traceback()
            )

    frappe.db.commit()
    
    elapsed = time.time() - start_time
    frappe.logger().info(
        f"✅ Import completed in {elapsed:.1f}s. "
        f"Created: {created_count}, Updated: {updated_count}, Skipped: {skipped_count}, Failed: {failure_count}"
    )


def _check_duplicate_barcode(barcode, item_code=None, cache=None):
    """
    Check if barcode already exists in the system.
    Returns tuple: (is_duplicate, existing_item_code)
    """
    if not barcode:
        return False, None
    
    # Check cache first
    if cache and 'barcodes' in cache:
        if barcode in cache['barcodes']:
            existing_item = cache['barcodes'][barcode]
            if item_code and existing_item == item_code:
                return False, None
            return True, existing_item
    
    # Check database
    existing = frappe.db.get_value(
        "Item Barcode",
        {"barcode": barcode},
        ["parent"],
        as_dict=True
    )
    
    if existing:
        # Cache the result
        if cache:
            cache['barcodes'][barcode] = existing.parent
        # If we're updating the same item, it's not a duplicate
        if item_code and existing.parent == item_code:
            return False, None
        return True, existing.parent
    
    # Cache negative result too
    if cache:
        cache['barcodes'][barcode] = None
    
    return False, None


def _handle_barcodes(item_doc, row_data, barcode_update_mode, is_new_item, row_idx, cache):
    """
    Handle barcode updates based on custom_update_existing_barcodes setting.
    
    For NEW items: Always create barcode (if not duplicate)
    For EXISTING items: Follow barcode_update_mode setting
    
    Returns: (success, error_message)
    """
    barcode = row_data.get("barcodes.barcode") or row_data.get("item_code")
    if not barcode:
        return True, None
    
    # Check for duplicate barcode
    is_duplicate, existing_item = _check_duplicate_barcode(barcode, item_doc.item_code, cache)
    
    if is_duplicate:
        error_msg = f"Duplicate barcode '{barcode}' already exists on item '{existing_item}'"
        frappe.logger().warning(f"Row {row_idx}: {error_msg}")
        return False, error_msg
    
    # For NEW items: Always create barcode, ignore the update mode
    if is_new_item:
        # Only add if not already added (item_code is default barcode)
        if barcode != item_doc.item_code:
            existing_barcodes = [b.barcode for b in item_doc.get("barcodes", [])]
            if barcode not in existing_barcodes:
                item_doc.append("barcodes", {"barcode": barcode})
                item_doc.save(ignore_permissions=True)
                # Cache the new barcode
                if cache:
                    cache['barcodes'][barcode] = item_doc.item_code
        return True, None
    
    # For EXISTING items: Follow the update mode
    existing_barcodes = [b.barcode for b in item_doc.get("barcodes", [])]
    
    if barcode_update_mode == "No Barcode Update":
        # Don't touch barcodes for existing items
        return True, None
    
    elif barcode_update_mode == "Replace Existing Barcodes":
        # Remove all existing barcodes and add new one
        if existing_barcodes:
            item_doc.set("barcodes", [])
        
        # Add new barcode if not already the item_code
        if barcode != item_doc.item_code:
            item_doc.append("barcodes", {"barcode": barcode})
            item_doc.save(ignore_permissions=True)
            if cache:
                cache['barcodes'][barcode] = item_doc.item_code
        return True, None
    
    elif barcode_update_mode == "Add Barcode to Existing List":
        # Only add if barcode doesn't already exist
        if barcode not in existing_barcodes:
            item_doc.append("barcodes", {"barcode": barcode})
            item_doc.save(ignore_permissions=True)
            if cache:
                cache['barcodes'][barcode] = item_doc.item_code
        return True, None
    
    return True, None


def _ensure_or_update_item(row_data, item_group_name, brand_name, attributes, cache, update_existing):
    item_code = row_data.get("item_code")
    variant_of = row_data.get("variant_of")
    item_name = row_data.get("item_name")
    is_variant = bool(variant_of)

    # ── ALWAYS handle template first for variants, regardless of what happens to the variant ──
    if is_variant:
        _handle_template(
            template_code=variant_of,
            item_name=item_name,
            row_data=row_data,
            item_group_name=item_group_name,
            brand_name=brand_name,
            attributes=attributes,
            cache=cache,
            update_existing=update_existing,
        )

    # ── Cache hit ──
    if item_code in cache['items']:
        item_doc = cache['items'][item_code]
        action = getattr(item_doc, '_import_action', 'Skipped')
        return item_doc, action

    # ── DB check ──
    existing = frappe.db.get_value("Item", filters={"item_code": ["=", item_code]}, fieldname="name")

    if existing:
        item_doc = frappe.get_doc("Item", existing)
        if update_existing:
            item_doc = _update_item_doc(item_doc, row_data, item_group_name, brand_name)
            item_doc._import_action = "Updated"
        else:
            item_doc._import_action = "Skipped"
        cache['items'][item_code] = item_doc
        return item_doc, item_doc._import_action

    # ── Create new ──
    common_fields = {
        "doctype": "Item",
        "item_code": item_code,
        "item_name": item_name,
        "item_group": item_group_name,
        "brand": brand_name,
        "is_stock_item": int(row_data.get("is_stock_item") or 0),
        "disabled": int(row_data.get("disabled") or 0),
        "is_sales_item": int(row_data.get("is_sales_item") or 1),
        "description": row_data.get("description"),
        "custom_item_name_ar": row_data.get("custom_item_name_ar"),
        "custom_style_code": row_data.get("custom_style_code"),
        "custom_material": row_data.get("custom_material"),
        "custom_image_url": row_data.get("custom_image_url"),
        "custom_bin_no": row_data.get("custom_bin_no"),
        "custom_dcs": row_data.get("custom_dcs"),
        "custom_vendor_code": row_data.get("custom_vendor_code"),
        "custom_vendor_name": row_data.get("custom_vendor_name"),
        "custom_vendor_currency": row_data.get("custom_vendor_currency"),
        "custom_exchange_rate": row_data.get("custom_exchange_rate"),
        "custom_vendor_cost": row_data.get("custom_vendor_cost"),
        "valuation_rate": row_data.get("valuation_rate"),
        "custom_last_synced": now_datetime(),
    }

    if is_variant:
        # Template already handled above — just get it from cache
        template_doc = cache['items'][variant_of]
        item_doc = frappe.get_doc(common_fields.copy())
        item_doc.variant_of = template_doc.name
        item_doc.has_variants = 0
        item_doc.attributes = []
        for field, value in attributes.items():
            item_doc.append("attributes", {"attribute": field, "attribute_value": value})
        item_doc.insert(ignore_permissions=True)
    else:
        item_doc = frappe.get_doc(common_fields)
        item_doc.insert(ignore_permissions=True)

    item_doc._import_action = "Created"
    cache['items'][item_code] = item_doc
    return item_doc, "Created"


def _handle_template(template_code, item_name, row_data, item_group_name, brand_name, attributes, cache, update_existing):
    """
    Handle template creation or update completely separately.
    
    Bugs fixed:
    1. Stale doc cached after reload() — now cache the reloaded doc
    2. Premature frappe.db.commit() inside loop removed
    3. Unnecessary verify fetch removed
    """

    # Check if template is in cache
    if template_code in cache['items']:
        template_doc = cache['items'][template_code]
        return template_doc

    # Check database
    template_name = frappe.db.get_value("Item", {"item_code": template_code}, "name")

    if not template_name:
        # CREATE NEW TEMPLATE
        template_fields = {
            "doctype": "Item",
            "item_code": template_code,
            "item_name": item_name,
            "item_group": item_group_name,
            "brand": brand_name,
            "is_stock_item": int(row_data.get("is_stock_item") or 0),
            "disabled": int(row_data.get("disabled") or 0),
            "is_sales_item": int(row_data.get("is_sales_item") or 1),
            "description": row_data.get("description"),
            "custom_item_name_ar": row_data.get("custom_item_name_ar"),
            "custom_style_code": row_data.get("custom_style_code"),
            "custom_material": row_data.get("custom_material"),
            "custom_image_url": row_data.get("custom_image_url"),
            "custom_bin_no": row_data.get("custom_bin_no"),
            "custom_dcs": row_data.get("custom_dcs"),
            "custom_vendor_code": row_data.get("custom_vendor_code"),
            "custom_vendor_name": row_data.get("custom_vendor_name"),
            "custom_vendor_currency": row_data.get("custom_vendor_currency"),
            "custom_exchange_rate": row_data.get("custom_exchange_rate"),
            "custom_vendor_cost": row_data.get("custom_vendor_cost"),
            "valuation_rate": row_data.get("valuation_rate"),
            "custom_last_synced": now_datetime(),
            "has_variants": 1,
            "variant_based_on": "Item Attribute",
        }

        template_doc = frappe.get_doc(template_fields)
        for field in attributes.keys():
            template_doc.append("attributes", {"attribute": field, "numeric_values": 0})

        template_doc.insert(ignore_permissions=True)
        template_doc._import_action = "Created"
        cache['items'][template_code] = template_doc
        cache.setdefault('updated_templates', set()).add(template_doc.name)
        return template_doc

    else:
        # TEMPLATE EXISTS
        already_updated = template_name in cache.get('updated_templates', set())

        if update_existing and not already_updated:
            # FIX 1: reload() returns nothing — you must use the doc IN PLACE after reload
            template_doc = frappe.get_doc("Item", template_name)
            template_doc.reload()  # now safe: we ARE using this reloaded doc

            update_fields = {
                "item_name": item_name,
                "item_group": item_group_name,
                "brand": brand_name,
                "is_stock_item": int(row_data.get("is_stock_item") or 0),
                "disabled": int(row_data.get("disabled") or 0),
                "is_sales_item": int(row_data.get("is_sales_item") or 1),
                "description": row_data.get("description"),
                "custom_item_name_ar": row_data.get("custom_item_name_ar"),
                "custom_style_code": row_data.get("custom_style_code"),
                "custom_material": row_data.get("custom_material"),
                "custom_image_url": row_data.get("custom_image_url"),
                "custom_bin_no": row_data.get("custom_bin_no"),
                "custom_dcs": row_data.get("custom_dcs"),
                "custom_vendor_code": row_data.get("custom_vendor_code"),
                "custom_vendor_name": row_data.get("custom_vendor_name"),
                "custom_vendor_currency": row_data.get("custom_vendor_currency"),
                "custom_exchange_rate": row_data.get("custom_exchange_rate"),
                "custom_vendor_cost": row_data.get("custom_vendor_cost"),
                "valuation_rate": row_data.get("valuation_rate"),
                "custom_last_synced": now_datetime(),
            }

            for field, value in update_fields.items():
                if value is not None and value != "":
                    setattr(template_doc, field, value)

            template_doc.save(ignore_permissions=True)
            # FIX 2: No frappe.db.commit() here — let the batch commit handle it

            template_doc._import_action = "Updated"
            # FIX 3: Cache by template_name (the doc.name), not template_code,
            #         so the already_updated check works correctly on next hit
            cache['items'][template_code] = template_doc
            cache.setdefault('updated_templates', set()).add(template_name)  # use template_name not template_doc.name

        elif already_updated:
            template_doc = cache['items'].get(template_code) or frappe.get_doc("Item", template_name)
            template_doc._import_action = "Updated"
            cache['items'][template_code] = template_doc

        else:
            template_doc = frappe.get_doc("Item", template_name)
            template_doc._import_action = "Skipped"
            cache['items'][template_code] = template_doc

        return template_doc



def _update_item_doc(item_doc, row_data, item_group_name, brand_name):
    """
    Update item document fields.
    IMPORTANT: Does NOT update variant_of, attributes, has_variants, or variant_based_on
    This applies to both regular items AND templates
    """
    # Update basic fields
    update_fields = {
        "item_name": row_data.get("item_name"),
        "item_group": item_group_name,
        "brand": brand_name,
        "is_stock_item": int(row_data.get("is_stock_item") or 0),
        "disabled": int(row_data.get("disabled") or 0),
        "is_sales_item": int(row_data.get("is_sales_item") or 1),
        "description": row_data.get("description"),
        "custom_item_name_ar": row_data.get("custom_item_name_ar"),  # FIXED: Changed from custom_item_name_arabic
        "custom_style_code": row_data.get("custom_style_code"),
        "custom_material": row_data.get("custom_material"),
        "custom_image_url": row_data.get("custom_image_url"),
        "custom_bin_no": row_data.get("custom_bin_no"),
        "custom_dcs": row_data.get("custom_dcs"),
        "custom_vendor_code": row_data.get("custom_vendor_code"),
        "custom_vendor_name": row_data.get("custom_vendor_name"),
        "custom_vendor_currency": row_data.get("custom_vendor_currency"),
        "custom_exchange_rate": row_data.get("custom_exchange_rate"),
        "custom_vendor_cost": row_data.get("custom_vendor_cost"),
        "valuation_rate": row_data.get("valuation_rate"),
        "custom_last_synced": now_datetime(),
    }
    
    # Only update if value provided (not empty)
    for field, value in update_fields.items():
        if value is not None and value != "":
            setattr(item_doc, field, value)
    
    # NEVER update these fields (for both items and templates)
    # - variant_of
    # - attributes  
    # - has_variants
    # - variant_based_on
    
    item_doc.save(ignore_permissions=True)
    return item_doc


def _collect_item_prices(item_doc, row_data, item_prices_batch, cache, is_update=False):
    """Collect item prices for batch processing"""
    price_map = {
        "PriceLevel3": "PriceLevel3",
        "PriceLevel1": "PriceLevel1",
        "PriceLevel2": "PriceLevel2",
    }
    currency = frappe.defaults.get_global_default("currency") or "SAR"
    
    for field, price_list in price_map.items():
        rate = row_data.get(field)
        if not rate:
            continue
        
        cache_key = f"{item_doc.item_code}:{price_list}"
        
        # If updating, we want to update even if already processed
        # If not updating (new item), skip if already in cache
        if not is_update and cache_key in cache['item_prices']:
            continue
            
        item_prices_batch.append({
            'item_code': item_doc.item_code,
            'price_list': price_list,
            'price_list_rate': float(rate),
            'currency': currency,
            'is_update': is_update
        })
        cache['item_prices'][cache_key] = True


def _process_item_prices_batch(item_prices_batch, update_existing_prices):
    """Process item prices in batch using bulk SQL operations"""
    if not item_prices_batch:
        return
    
    # Group by item_code and price_list to avoid duplicates within batch
    seen = set()
    unique_prices = []
    for price in item_prices_batch:
        key = (price['item_code'], price['price_list'])
        if key not in seen:
            seen.add(key)
            unique_prices.append(price)
    
    # Process each price
    for price in unique_prices:
        existing = frappe.db.get_value(
            "Item Price",
            {"item_code": price['item_code'], "price_list": price['price_list']},
            "name"
        )
        
        if existing:
            if update_existing_prices or price.get('is_update'):
                # Update existing price
                frappe.db.set_value("Item Price", existing, "price_list_rate", price['price_list_rate'])
        else:
            # Create new price
            ip = frappe.get_doc({
                "doctype": "Item Price",
                "item_code": price['item_code'],
                "price_list": price['price_list'],
                "price_list_rate": price['price_list_rate'],
                "currency": price['currency'],
            })
            ip.insert(ignore_permissions=True)


def _create_purchase_order(log_doc):
    """
    Create Purchase Orders grouped by supplier.
    Each supplier gets one PO with all items belonging to them.
    Supports multi-currency with currency and exchange_rate fields.
    """
    from frappe.utils import nowdate

    supplier_map = {}  # {supplier: {currency: {rate: rate, items: []}}}

    for entry in log_doc.entries:
        if entry.status != "Success":
            continue

        row = frappe.parse_json(entry.row_data)

        supplier = row.get("supplier_items.supplier")
        item_code = str(row.get("item_code")).strip()
        qty = float(row.get("po_qty") or 0)
        price_list_rate = float(row.get("po_price") or 0)
        rate = float(row.get("po_price") or 0)
        currency = (
            row.get("currency")
            or frappe.defaults.get_global_default("currency")
            or "SAR"
        )
        exchange_rate = float(row.get("exchange_rate") or 1.0)
        set_warehouse = "Main Warehouse - MAATC"

        if not supplier or not item_code or qty <= 0:
            continue

        # Group by supplier and currency
        key = (supplier, currency)
        if key not in supplier_map:
            supplier_map[key] = {"exchange_rate": exchange_rate, "items": []}

        supplier_map[key]["items"].append(
            {
                "item_code": item_code,
                "qty": qty,
                "rate": rate,
                "price_list_rate": price_list_rate,
            }
        )

    frappe.logger().info(f"✅ Inserting PO")

    # Create PO for each supplier-currency combination
    for (supplier, currency), data in supplier_map.items():
        po = frappe.get_doc(
            {
                "doctype": "Purchase Order",
                "supplier": supplier,
                "transaction_date": nowdate(),
                "schedule_date": nowdate(),
                "currency": currency,
                "conversion_rate": data["exchange_rate"],
                "set_warehouse": set_warehouse,
                "items": [],
            }
        )

        for it in data["items"]:
            po.append(
                "items",
                {
                    "item_code": it["item_code"],
                    "qty": it["qty"],
                    "price_list_rate": it["price_list_rate"],
                    "rate": it["rate"],
                    "schedule_date": nowdate(),
                },
            )

        frappe.logger().info(f"✅ Creating PO {po.supplier} with currency {currency}")
        po.insert(ignore_permissions=True)
        # po.submit()


def _row_to_dict(row, col_index):
    data = {}
    for fieldname, idx in col_index.items():
        data[fieldname] = row[idx] if idx < len(row) else None
    return data


def sanitize_row(row_data):
    for key, value in row_data.items():
        if isinstance(value, int) or isinstance(value, float):
            row_data[key] = str(value)
        elif value is None:
            row_data[key] = ""
        else:
            row_data[key] = str(value).strip()
    return row_data


def _validate_mandatory(row_data, fields, row_idx):
    missing = [f for f in fields if not row_data.get(f)]
    if missing:
        frappe.throw(f"Row {row_idx}: Missing mandatory fields: {', '.join(missing)}")

def _ensure_item_group_hierarchy(row_data, cache):
    lvl1 = row_data.get("Group-Level1")
    lvl2 = row_data.get("Group-Level2")
    lvl3 = row_data.get("Group-Level3")

    if not all([lvl1, lvl2, lvl3]):
        frappe.throw("Item Group levels 1-3 are required.")

    cache_key = f"{lvl1}.{lvl2}.{lvl3}"
    if cache_key in cache['item_groups']:
        return cache['item_groups'][cache_key]

    def get_or_create_group(name, parent_item_group, is_group):
        if name in cache['item_groups']:
            return cache['item_groups'][name]

        existing = frappe.db.get_value("Item Group", {"item_group_name": name})
        if existing:
            cache['item_groups'][name] = existing
            return existing

        # Extract the display label — just the last segment after the last dot
        # e.g. "Women.Accessories.Water Bottle" → "Water Bottle"
        display_name = name.split(".")[-1]

        doc = frappe.get_doc({
            "doctype": "Item Group",
            "item_group_name": name,
            "parent_item_group": "All Item Groups" if not parent_item_group else parent_item_group,
            "is_group": 1 if is_group else 0,
            # Fill both display name fields with the leaf segment
            "custom_displayname": display_name,
            "custom_item_group_display_name": display_name,
        })
        doc.insert(ignore_permissions=True)
        cache['item_groups'][name] = doc.name
        return doc.name

    ig1 = get_or_create_group(lvl1, None, True)
    ig2 = get_or_create_group(f"{lvl1}.{lvl2}", ig1, True)
    ig3 = get_or_create_group(f"{lvl1}.{lvl2}.{lvl3}", ig2, False)

    cache['item_groups'][cache_key] = ig3
    return ig3

def _ensure_brand(row_data, cache):
    brand_name = row_data.get("brand")
    if not brand_name:
        frappe.throw("Brand is mandatory.")
    
    # Check cache first
    if brand_name in cache['brands']:
        return cache['brands'][brand_name]

    existing = frappe.db.get_value("Brand", {"brand": brand_name})
    if existing:
        cache['brands'][brand_name] = existing
        return existing

    doc = frappe.get_doc({"doctype": "Brand", "brand": brand_name})
    doc.insert(ignore_permissions=True)
    cache['brands'][brand_name] = doc.name
    return doc.name


def _ensure_supplier(row_data, cache):
    supplier_name = row_data.get("supplier_items.supplier")
    if not supplier_name:
        frappe.throw("Supplier is mandatory.")
    
    # Check cache first
    if supplier_name in cache['suppliers']:
        return cache['suppliers'][supplier_name]

    existing = frappe.db.get_value("Supplier", {"name": supplier_name})
    if existing:
        cache['suppliers'][supplier_name] = existing
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_group": "All Supplier Groups",
            "supplier_type": "Company",
        }
    )
    doc.insert(ignore_permissions=True)
    cache['suppliers'][supplier_name] = doc.name
    return doc.name


def _ensure_attributes(row_data, cache):
    attr_map = {
        "Color": "Color",
        "Color Code": "Color Code",
        "Size": "Size",
        "Season": "Season",
        "Assortment": "Assortment",
    }
    result = {}
    for field, attr_name in attr_map.items():
        value = row_data.get(field)
        if not value:
            continue
        attr = _get_or_create_attribute(attr_name, cache)
        _get_or_create_attribute_value(attr, value, cache)
        result[field] = value
    return result


def _get_or_create_attribute(attribute_name, cache):
    # Check cache first
    if attribute_name in cache['attributes']:
        return cache['attributes'][attribute_name]
        
    existing = frappe.db.get_value("Item Attribute", {"attribute_name": attribute_name})
    if existing:
        cache['attributes'][attribute_name] = existing
        return existing
        
    doc = frappe.get_doc(
        {
            "doctype": "Item Attribute",
            "attribute_name": attribute_name,
            "item_attribute_values": [],
        }
    )
    doc.insert(ignore_permissions=True)
    cache['attributes'][attribute_name] = doc.name
    return doc.name


def _get_or_create_attribute_value(attribute_name, value, cache):
    normalized_value = str(value).strip()
    cache_key = f"{attribute_name}:{normalized_value}"
    
    # Check cache first
    if cache_key in cache['attribute_values']:
        return cache['attribute_values'][cache_key]

    # Check if value already exists in database
    exists = frappe.db.get_value(
        "Item Attribute Value",
        {"parent": attribute_name, "attribute_value": normalized_value},
    )
    if exists:
        cache['attribute_values'][cache_key] = exists
        return exists

    # Reload to get latest state (in case another process added values)
    attr_doc = frappe.get_doc("Item Attribute", attribute_name)

    # Check in-memory to avoid duplicates within same transaction
    existing_values = [v.attribute_value for v in attr_doc.item_attribute_values]
    if normalized_value in existing_values:
        cache['attribute_values'][cache_key] = normalized_value
        return normalized_value

    try:
        attr_doc.append(
            "item_attribute_values",
            {"attribute_value": normalized_value, "abbr": normalized_value[:10]},
        )
        attr_doc.save(ignore_permissions=True)
        cache['attribute_values'][cache_key] = normalized_value
    except frappe.exceptions.ValidationError as e:
        if "must appear only once" in str(e):
            # Another process added it, fetch and return
            result = frappe.db.get_value(
                "Item Attribute Value",
                {"parent": attribute_name, "attribute_value": normalized_value},
            )
            cache['attribute_values'][cache_key] = result
            return result
        raise

    return normalized_value