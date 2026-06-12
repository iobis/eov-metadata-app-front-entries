import json
import re
import os

def strip_schema_prefixes(obj):
    """
    Recursively strip 'schema:' prefix from dictionary keys.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            new_key = k[7:] if isinstance(k, str) and k.startswith("schema:") else k
            out[new_key] = strip_schema_prefixes(v)
        return out
    elif isinstance(obj, list):
        return [strip_schema_prefixes(item) for item in obj]
    else:
        return obj

def normalize_to_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def make_safe_name(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        raw = "entry"
    safe = re.sub(r'[^\w\-_\.]+', '_', raw)
    safe = re.sub(r'[_.]{2,}', '_', safe).strip('_.')
    safe = safe[:50] if len(safe) > 50 else safe
    return safe or 'entry'


def extract_maintenance_frequency(additional_property):
    if isinstance(additional_property, dict):
        additional_property = [additional_property]
    if isinstance(additional_property, list):
        for prop in additional_property:
            if isinstance(prop, dict) and prop.get('name') == 'maintenanceFrequency':
                return str(prop.get('value', 'unknown'))
    return 'unknown'


def load_eov_lookup(schema_file):
    """Build a lookup from normalized EOV name to canonical propertyID(s) from schema.json."""
    try:
        with open(schema_file, 'r', encoding='utf-8') as f:
            schema = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    lookup = {}
    fields = (schema
              .get('categories_definition', {})
              .get('variable_measured', {})
              .get('fields', {}))
    for field_data in fields.values():
        for opt in field_data.get('options', {}).values():
            name = opt.get('name', '').strip()
            prop = opt.get('propertyID')
            if name and prop is not None:
                lookup[name.lower()] = prop
    return lookup


def keyword_from_variable_measured(vm, eov_lookup=None):
    if not isinstance(vm, dict):
        return None
    name = vm.get('name')
    source_url = vm.get('propertyID') or vm.get('url') or vm.get('identifier')
    if not name and not source_url:
        return None

    canonical_url = None
    if eov_lookup and name:
        canonical_url = eov_lookup.get(name.lower().strip())

    url = canonical_url if canonical_url is not None else source_url

    keyword = {
        "@type": "schema:DefinedTerm",
        "schema:name": name or "",
    }
    if url is not None:
        keyword["schema:url"] = url
    if isinstance(vm.get('identifier'), str):
        keyword["schema:identifier"] = vm.get('identifier')
    if isinstance(vm.get('termCode'), str):
        keyword["schema:termCode"] = vm.get('termCode')
    return keyword


def extract_wkt(geometry):
    if not isinstance(geometry, dict):
        return None
    wkt_candidate = geometry.get('geosparql:asWKT') or geometry.get('asWKT')
    if isinstance(wkt_candidate, str):
        return wkt_candidate
    if isinstance(wkt_candidate, dict):
        return wkt_candidate.get('@value')
    return None


def is_likely_bounding_box_wkt(wkt_value):
    if not isinstance(wkt_value, str):
        return False
    match = re.search(r'POLYGON\s*\(\((.*?)\)\)', wkt_value, re.IGNORECASE | re.DOTALL)
    if not match:
        return False
    coordinates = [coord.strip() for coord in match.group(1).split(',') if coord.strip()]
    return len(coordinates) == 5


def prefix_schema_keys(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == '@type' or k.startswith('schema:') or k.startswith('geosparql:'):
                out[k] = prefix_schema_keys(v)
            else:
                out[f'schema:{k}'] = prefix_schema_keys(v)
        return out
    elif isinstance(obj, list):
        return [prefix_schema_keys(item) for item in obj]
    return obj


def transform_to_form_format(entry, seen_names=None, index=0, eov_lookup=None):
    """
    Transform a ResearchProject entry to the form output format.
    """
    if seen_names is None:
        seen_names = {}

    # Strip schema: prefixes only. Keep geosparql keys intact.
    stripped = strip_schema_prefixes(entry)

    name = stripped.get('name') or stripped.get('@id') or f'entry_{index}'
    base_name = make_safe_name(name)
    safe_name = base_name
    count = seen_names.get(base_name, 0)
    while safe_name in seen_names and seen_names[safe_name] > 0:
        count += 1
        safe_name = f"{base_name}_{count}"
    seen_names[base_name] = count
    seen_names[safe_name] = 1

    project_id = f"https://raw.githubusercontent.com/iobis/eov-metadata-app-front-entries/refs/heads/main/jsonFiles/{safe_name}/{safe_name}.json"

    frequency = extract_maintenance_frequency(stripped.get('additionalProperty'))

    vm_list = normalize_to_list(stripped.get('variableMeasured'))
    if vm_list:
        keywords = []
        for vm in vm_list:
            kw = keyword_from_variable_measured(vm, eov_lookup=eov_lookup)
            if kw is not None:
                keywords.append(kw)
    else:
        keywords = []
        for item in normalize_to_list(stripped.get('keywords')):
            if isinstance(item, dict) and eov_lookup:
                name = item.get('name', '')
                if name:
                    canonical = eov_lookup.get(name.lower().strip())
                    if canonical is not None:
                        item = dict(item)
                        item['url'] = canonical
            keywords.append(item)
    keywords = [prefix_schema_keys(item) if isinstance(item, dict) else item for item in keywords]

    area_served = []
    geo_obj = stripped.get('geosparql:hasGeometry')
    wkt_value = extract_wkt(geo_obj)
    place = None
    if isinstance(geo_obj, dict):
        place = {"@type": "schema:Place"}
        if isinstance(geo_obj.get('name'), str) and geo_obj.get('name').strip():
            place["schema:name"] = geo_obj.get('name').strip()
        if isinstance(geo_obj.get('identifier'), str) and geo_obj.get('identifier').strip():
            place["schema:identifier"] = geo_obj.get('identifier').strip()

    if wkt_value and is_likely_bounding_box_wkt(wkt_value):
        if place is None:
            place = {"@type": "schema:Place"}
        place["schema:geo"] = {
            "@type": "schema:GeoShape",
            "schema:description": "Bounding box polygon with lat long (Y X) coordinate order.",
            "geosparql:asWKT": {
                "@type": "http://www.opengis.net/ont/geosparql#wktLiteral",
                "@value": wkt_value
            }
        }

    if place and len(place) > 1:
        area_served.append(place)

    if wkt_value and not is_likely_bounding_box_wkt(wkt_value):
        area_served.append({
            "geosparql:hasGeometry": {
                "geosparql:asWKT": {
                    "@type": "http://www.opengis.net/ont/geosparql#wktLiteral",
                    "@value": wkt_value
                }
            }
        })

    founding_date = ""
    temporal = stripped.get('temporalCoverage') or stripped.get('foundingDate')
    if temporal:
        match = re.search(r'(\d{4}-\d{2}-\d{2})', str(temporal))
        if match:
            founding_date = match.group(1)
        else:
            founding_date = str(temporal)

    publishing_principles = [prefix_schema_keys(item) if isinstance(item, dict) else item for item in normalize_to_list(stripped.get('publishingPrinciples'))]
    funding = [prefix_schema_keys(item) if isinstance(item, dict) else item for item in normalize_to_list(stripped.get('funding'))]
    contact_point = [prefix_schema_keys(item) if isinstance(item, dict) else item for item in normalize_to_list(stripped.get('contactPoint'))]
    makes_offer = [prefix_schema_keys(item) if isinstance(item, dict) else item for item in normalize_to_list(stripped.get('makesOffer'))]

    # Map hasPart to makesOffer
    has_part = stripped.get('hasPart')
    if has_part:
        has_part_list = normalize_to_list(has_part)
        for part in has_part_list:
            if isinstance(part, dict) and part.get('contentUrl'):
                offer = {
                    "@type": "schema:Offer",
                    "schema:name": "",
                    "schema:itemOffered": {
                        "@type": "schema:CreativeWork",
                        "schema:name": "",
                        "schema:url": part.get('contentUrl')
                    }
                }
                makes_offer.append(prefix_schema_keys(offer))

    parent_org = stripped.get('parentOrganization')
    if not isinstance(parent_org, dict):
        parent_org = {
            "schema:legalName": "",
            "schema:url": ""
        }
    else:
        parent_org = prefix_schema_keys(parent_org)

    project = {
        "@type": "Project",
        "schema:legalName": stripped.get('legalName', stripped.get('name', '')),
        "schema:name": stripped.get('name', ''),
        "schema:url": stripped.get('url', ''),
        "schema:description": stripped.get('description', ''),
        "schema:identifier": stripped.get('identifier', {}),
        "schema:parentOrganization": parent_org,
        "schema:publishingPrinciples": publishing_principles,
        "schema:foundingDate": founding_date,
        "schema:dissolutionDate": stripped.get('dissolutionDate', ''),
        "schema:areaServed": area_served,
        "@id": project_id,
        "schema:keywords": keywords,
        "schema:funding": funding,
        "schema:contactPoint": contact_point,
        "schema:makesOffer": makes_offer
    }

    action = {
        "@type": "schema:Action",
        "agent": {
            "@type": "schema:ResearchProject",
            "@id": project_id,
            "name": stripped.get('name', '')
        },
        "@id": project_id,
        "schema:description": frequency,
        "instrument": normalize_to_list(stripped.get('instrument')),
        "actionProcess": normalize_to_list(stripped.get('actionProcess'))
    }

    return safe_name, [project, action, {"schema:frequency": "unknown"}]

def convert_bioeco_graph(input_file, output_dir="jsonFiles", schema_file="schema.json"):
    """
    Convert bioeco_graph.jsonld to separate JSON files for each entry.
    """
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    graph = data.get('@graph', [])
    eov_lookup = load_eov_lookup(schema_file)
    
    context = {
        "schema": "http://schema.org/",
        "geosparql": "http://www.opengis.net/ont/geosparql#"
    }
    
    seen_names = {}
    stats = {
        'total': len(graph),
        'missing_name': 0,
        'duplicate_name': 0,
        'converted': 0,
        'failed': 0,
    }

    for index, entry in enumerate(graph):
        try:
            safe_name, graph_data = transform_to_form_format(entry, seen_names=seen_names, index=index, eov_lookup=eov_lookup)
            filename = os.path.join(output_dir, safe_name, f"{safe_name}.json")
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            data_out = {
                "@context": context,
                "@graph": graph_data
            }
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data_out, f, indent=2, ensure_ascii=False)
            stats['converted'] += 1
        except Exception:
            stats['failed'] += 1

    print(f"Converted {stats['converted']} of {stats['total']} entries to separate JSON files in {output_dir}")
    if stats['failed']:
        print(f"Failed to convert {stats['failed']} entries")

if __name__ == "__main__":
    import sys
    input_file = "bioeco_graph.jsonld"
    output_dir = "jsonFiles"
    schema_file = "schema.json"
    if len(sys.argv) > 1:
        input_file = sys.argv[1]
    if len(sys.argv) > 2:
        output_dir = sys.argv[2]
    if len(sys.argv) > 3:
        schema_file = sys.argv[3]
    convert_bioeco_graph(input_file, output_dir, schema_file=schema_file)