"""Read-only SQLite catalog and checked query-pack engine."""
from __future__ import annotations
import hashlib, json, os, re, sqlite3, time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get("VOODOOSQL_HOME", Path.home() / ".voodoosql"))
FIXTURE = RUNTIME / "demo.sqlite"
METADATA_DB = RUNTIME / "catalog.sqlite"
SCHEMA_VERSION = "1"

def connect_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(path).resolve()
    if not p.is_file():
        raise ValueError("Database file does not exist")
    uri = p.as_uri() + "?mode=ro"
    c = sqlite3.connect(uri, uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c

@dataclass
class CatalogSnapshot:
    tables: list[dict]
    relations: list[dict]
    fingerprint: str
    completeness: dict
    def to_dict(self):
        return {"tables": self.tables, "relations": self.relations, "fingerprint": self.fingerprint, "completeness": self.completeness}
    def to_graph(self):
        nodes=[]; edges=[]
        for t in self.tables:
            nodes.append({"id":"table:"+t["name"],"type":"table","label":t["name"]})
            for col in t["columns"]:
                nodes.append({"id":"column:"+t["name"]+"."+col["name"],"type":"column","label":col["name"],"table":t["name"],"data_type":col["type"],"primary_key_position":col["primary_key_position"]})
                edges.append({"source":"table:"+t["name"],"target":"column:"+t["name"]+"."+col["name"],"type":"HAS_COLUMN","evidence":"extracted"})
        for r in self.relations:
            edges.append({"source":"table:"+r["from"],"target":"table:"+r["to"],"type":"RELATES_TO","from_columns":r["from_columns"],"to_columns":r["to_columns"],"evidence":r["evidence"],"status":r["status"],"enforcement_status":r.get("enforcement_status","unknown")})
        return {"format":"graphify-compatible-v1","nodes":nodes,"edges":edges,"fingerprint":self.fingerprint}

def save_catalog(snapshot, path=METADATA_DB):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    c=sqlite3.connect(path)
    c.executescript("CREATE TABLE IF NOT EXISTS snapshots(fingerprint TEXT PRIMARY KEY, completeness TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP); CREATE TABLE IF NOT EXISTS objects(fingerprint TEXT NOT NULL, object_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(fingerprint,object_id)); CREATE TABLE IF NOT EXISTS relationships(fingerprint TEXT NOT NULL, relation_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(fingerprint,relation_id));")
    c.execute("INSERT OR IGNORE INTO snapshots(fingerprint,completeness) VALUES (?,?)",(snapshot.fingerprint,json.dumps(snapshot.completeness,sort_keys=True)))
    c.executemany("INSERT OR IGNORE INTO objects VALUES (?,?,?)",[(snapshot.fingerprint,x["name"],json.dumps(x,sort_keys=True)) for x in snapshot.tables])
    c.executemany("INSERT OR IGNORE INTO relationships VALUES (?,?,?)",[(snapshot.fingerprint,str(i),json.dumps(x,sort_keys=True)) for i,x in enumerate(snapshot.relations)])
    c.commit(); c.close(); os.chmod(path,0o600)

class Catalog:
    @classmethod
    def from_sqlite(cls, path=FIXTURE, allowed_schemas=None):
        # SQLite provides one database namespace; attached databases are never inspected.
        c = connect_readonly(path)
        schemas=["main"] if allowed_schemas is None else [s for s in allowed_schemas if s=="main"]
        complete="main" in schemas and (allowed_schemas is None or set(allowed_schemas).issubset({"main"}))
        names = [r[0] for r in c.execute("SELECT name FROM main.sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")] if "main" in schemas else []
        tables, relations = [], []
        for name in names:
            qname = '"' + name.replace('"', '""') + '"'
            cols = [dict(r) for r in c.execute(f"PRAGMA table_xinfo({qname})")]
            pks = sorted((x for x in cols if x['pk']), key=lambda x:x['pk'])
            tables.append({"name":name,"kind":c.execute("SELECT type FROM sqlite_schema WHERE name=?",(name,)).fetchone()[0],"columns":[{"name":x['name'],"type":x['type'],"nullable":not bool(x['notnull']),"primary_key_position":x['pk']} for x in cols],"grain":None})
            fkrows = list(c.execute(f"PRAGMA foreign_key_list({qname})"))
            groups = {}
            for row in fkrows: groups.setdefault(row['id'], []).append(dict(row))
            for rows in groups.values():
                rows.sort(key=lambda x:x['seq'])
                target=rows[0]['table']
                target_pk=sorted((x for x in c.execute(f'PRAGMA table_xinfo("{target.replace(chr(34), chr(34)*2)}")') if x['pk']),key=lambda x:x['pk'])
                to_cols=[x['to'] if x['to'] is not None else (target_pk[i]['name'] if i<len(target_pk) else None) for i,x in enumerate(rows)]
                relations.append({"from":name,"to":target,"from_columns":[x['from'] for x in rows],"to_columns":to_cols,"evidence":"declared","enforced":None,"enforcement_status":"unknown","inspection_connection_foreign_keys":bool(c.execute("PRAGMA foreign_keys").fetchone()[0]),"status":"declared","source":"SQLite foreign_key_list"})
        if "main" in schemas and {"orders", "customers"}.issubset(names) and c.execute("PRAGMA application_id").fetchone()[0]==1448309587:
            relations.append({"from":"orders","to":"customers","from_columns":["tenant_id","customer_id"],"to_columns":["tenant_id","customer_id"],"evidence":"inferred","enforced":None,"enforcement_status":"unknown","inspection_connection_foreign_keys":bool(c.execute("PRAGMA foreign_keys").fetchone()[0]),"status":"review_required","source":"fabricated demo design note"})
        definitions = [list(r) for r in c.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")] if "main" in schemas else []
        c.close()
        raw=json.dumps({"tables":tables,"relations":relations,"definitions":definitions},sort_keys=True,separators=(",",":"))
        return CatalogSnapshot(tables,relations,hashlib.sha256(raw.encode()).hexdigest(),{"complete":complete,"scope":schemas,"note":"SQLite file metadata reflects objects visible through this read-only connection. File permissions and application roles are outside this adapter. Enforcement is unknown; inspection_connection_foreign_keys records only this adapter connection."})

def _authorizer(action, arg1, arg2, db, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    if action not in allowed: return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        fn=(arg2 or arg1 or "").lower()
        if fn not in {"count","sum","avg","min","max","coalesce","ifnull","lower","upper","date","datetime","strftime","substr","length","round","abs","row_number"}:
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and db not in ("main", None): return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

def execute_checked(sql: str, params=None, *, path=FIXTURE, row_limit=200, time_limit_ms=1200, instruction_budget=200_000):
    if not isinstance(sql,str) or len(sql)>12000: raise ValueError("Query must be text under 12,000 characters")
    if params is None: params = {}
    if not isinstance(params, (dict,tuple,list)): raise ValueError("Parameters must be a mapping or sequence")
    # Reject multi-statement input; sqlite's authorizer remains the actual write boundary.
    if ";" in sql.rstrip().rstrip(";"): raise ValueError("Only one statement is allowed")
    if not re.match(r"\s*(SELECT|WITH)\b",sql,re.I): raise ValueError("Only SELECT queries are allowed")
    if not 1 <= row_limit <= 10000 or not 1 <= time_limit_ms <= 10000 or not 0 <= instruction_budget <= 10000000:
        raise ValueError("Query budgets exceed supported limits")
    c=connect_readonly(path); started=time.monotonic(); steps=[0]
    def progress():
        steps[0]+=1000
        return int(steps[0]>instruction_budget or (time.monotonic()-started)*1000>time_limit_ms)
    c.set_authorizer(_authorizer); c.set_progress_handler(progress,1000)
    try:
        cur=c.execute(sql,params)
        rows=cur.fetchmany(row_limit+1)
        if len(rows)>row_limit: raise ValueError(f"Result exceeds {row_limit} rows")
        return {"columns":[x[0] for x in cur.description or []],"rows":[list(r) for r in rows],"elapsed_ms":round((time.monotonic()-started)*1000,2),"instructions":steps[0]}
    finally: c.close()

RECIPES={
 "customer-orders": {"title":"Orders for a customer","sql":"SELECT order_id, ordered_at, status, total_cents FROM orders WHERE tenant_id = :tenant_id AND customer_id = :customer_id ORDER BY ordered_at DESC","params":{"tenant_id":"t1","customer_id":"c1"},"grain":"one row per order","join":"No join required. Composite customer identity is (tenant_id, customer_id).","evidence":"reviewed synthetic recipe"},
 "paid-by-order": {"title":"Paid total by order","sql":"SELECT o.order_id, o.total_cents, COALESCE(p.paid_cents, 0) AS paid_cents FROM orders o LEFT JOIN (SELECT tenant_id, order_id, SUM(amount_cents) paid_cents FROM payments WHERE status='settled' GROUP BY tenant_id, order_id) p ON p.tenant_id=o.tenant_id AND p.order_id=o.order_id ORDER BY o.order_id","params":{},"grain":"one row per order","join":"Aggregate payments to order grain before joining. Joining raw lines and payments multiplies both sides.","evidence":"reviewed synthetic recipe"},
 "history-current": {"title":"Current status per order","sql":"SELECT order_id, status, effective_at FROM (SELECT order_id,status,effective_at,ROW_NUMBER() OVER (PARTITION BY tenant_id,order_id ORDER BY effective_at DESC, history_id DESC) rn FROM order_history) WHERE rn=1 ORDER BY order_id","params":{},"grain":"one row per order","join":"Latest effective row; ties break on history_id.","evidence":"reviewed synthetic recipe"},
 "history-as-of": {"title":"Order status as of a time","sql":"SELECT order_id, status, effective_at FROM (SELECT order_id,status,effective_at,ROW_NUMBER() OVER (PARTITION BY tenant_id,order_id ORDER BY effective_at DESC, history_id DESC) rn FROM order_history WHERE effective_at <= :as_of) WHERE rn=1 ORDER BY order_id","params":{"as_of":"2026-02-01T10:30:00"},"grain":"one row per order with a history row by the cutoff","join":"Filter history to the cutoff before selecting the latest row. Orders with no history by the cutoff are omitted.","evidence":"reviewed synthetic recipe"}
 ,"history-as-of-order": {"title":"Join each order to its status at a time","sql":"SELECT o.order_id, o.status AS current_status, h.status AS status_at_cutoff, h.effective_at FROM orders o LEFT JOIN (SELECT tenant_id,order_id,status,effective_at,ROW_NUMBER() OVER (PARTITION BY tenant_id,order_id ORDER BY effective_at DESC, history_id DESC) rn FROM order_history WHERE effective_at <= :as_of) h ON h.tenant_id=o.tenant_id AND h.order_id=o.order_id AND h.rn=1 ORDER BY o.order_id","params":{"as_of":"2026-02-01T10:30:00"},"grain":"one row per order","join":"Filter history to the cutoff, select one latest history row, then join on the composite order key. Orders without an earlier history row remain with a null historical status.","evidence":"reviewed synthetic recipe"}
}

class QueryPack:
    def __init__(self,path=FIXTURE): self.path=path; self.fingerprint=Catalog.from_sqlite(path).fingerprint
    def retrieve(self,question):
        q=question.lower()
        if any(x in q for x in ("margin","active customer","net revenue")):
            return {"status":"clarification_required","question":"Which definition should I use for this metric, including refunds, status filters, currency, and time window?","context":[]}
        if any(x in q for x in ("as of","at time","historical")) and any(x in q for x in ("status","order")): key="history-as-of-order"
        elif any(x in q for x in ("history","current status","latest status")): key="history-current"
        elif any(x in q for x in ("paid","payment")): key="paid-by-order"
        elif any(x in q for x in ("order","customer")): key="customer-orders"
        else: return {"status":"clarification_required","question":"Which business question and output grain should I answer? Name the metric or records you need, and any time window or filters.","context":[]}
        recipe=RECIPES[key]
        return {"status":"ready","recipe_id":key,"recipe":recipe,"fingerprint":self.fingerprint}
    def execute(self,recipe_id,params=None):
        current=Catalog.from_sqlite(self.path).fingerprint
        if current!=self.fingerprint: return {"status":"stale","message":"Schema changed. Rebuild and review this query pack."}
        r=RECIPES[recipe_id]
        return {"status":"verified_fixture","recipe_id":recipe_id,"fingerprint":current,**execute_checked(r['sql'],params if params is not None else r['params'],path=self.path)}

def seed(path=FIXTURE):
    path=Path(path); path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    c=sqlite3.connect(path)
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers'").fetchone():
        c.execute("PRAGMA application_id=1448309587"); c.commit(); c.close(); os.chmod(path,0o600); return
    c.executescript('''
    PRAGMA application_id=1448309587;
    PRAGMA foreign_keys=ON;
    CREATE TABLE customers(tenant_id TEXT NOT NULL, customer_id TEXT NOT NULL, name TEXT, PRIMARY KEY(tenant_id,customer_id));
    CREATE TABLE orders(tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, customer_id TEXT NOT NULL, ordered_at TEXT NOT NULL, status TEXT NOT NULL, total_cents INTEGER NOT NULL, PRIMARY KEY(tenant_id,order_id));
    CREATE TABLE products(product_id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE order_lines(tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, line_no INTEGER NOT NULL, product_id TEXT NOT NULL, qty INTEGER NOT NULL, unit_cents INTEGER NOT NULL, PRIMARY KEY(tenant_id,order_id,line_no), FOREIGN KEY(tenant_id,order_id) REFERENCES orders(tenant_id,order_id));
    CREATE TABLE payments(payment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL, FOREIGN KEY(tenant_id,order_id) REFERENCES orders(tenant_id,order_id));
    CREATE TABLE order_history(history_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, status TEXT NOT NULL, effective_at TEXT NOT NULL, FOREIGN KEY(tenant_id,order_id) REFERENCES orders(tenant_id,order_id));
    CREATE TABLE tags(tag_id TEXT PRIMARY KEY, label TEXT NOT NULL);
    CREATE TABLE order_tags(tenant_id TEXT, order_id TEXT, tag_id TEXT, PRIMARY KEY(tenant_id,order_id,tag_id), FOREIGN KEY(tag_id) REFERENCES tags(tag_id));
    INSERT INTO customers VALUES ('t1','c1','Avery'),('t1','c2','Avery'),('t2','c1','Robin');
    INSERT INTO orders VALUES ('t1','o1','c1','2026-02-01','paid',3000),('t1','o2','c1','2026-02-02','paid',4000),('t2','o3','c1','2026-02-02','open',500);
    INSERT INTO products VALUES ('p1','Mug'),('p2','Notebook');
    INSERT INTO order_lines VALUES ('t1','o1',1,'p1',1,1000),('t1','o1',2,'p2',2,1000),('t1','o2',1,'p1',2,2000),('t2','o3',1,'p2',1,500);
    INSERT INTO payments VALUES ('pay1','t1','o1',1000,'settled'),('pay2','t1','o1',2000,'settled'),('pay3','t1','o2',4000,'settled');
    INSERT INTO order_history VALUES ('h1','t1','o1','open','2026-02-01T10:00:00'),('h2','t1','o1','paid','2026-02-01T11:00:00'),('h3','t1','o2','paid','2026-02-02T10:00:00'),('h4','t2','o3','open','2026-02-02T10:00:00');
    INSERT INTO tags VALUES ('tag1','gift'),('tag2','bulk'); INSERT INTO order_tags VALUES ('t1','o1','tag1'),('t1','o1','tag2');
    '''); c.commit(); c.close(); os.chmod(path,0o600)

if __name__=='__main__': seed()
