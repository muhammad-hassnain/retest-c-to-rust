// Differential driver, Rust side: drives PtrTrans's own Rust translation of quadtree
// (artifacts/ptrtrans/dataset/Trans_C-Rust-KG/quadtree, used unmodified as a path dependency)
// through the same public API as qt_oracle.c, from the same command script, printing the same
// format. See qt_oracle.c for the command language.
//
// Mapping of the C API to the translation's API (as PtrTrans emitted it):
//   void *key            -> Option<Box<dyn Any>> (a KeyBox(id); -1 -> None)
//   tree->key_free       -> Quadtree.key_free: Option<&dyn FnMut(&dyn Any)>  (command K)
//   quadtree_free / quadtree_node_free / *_free -> not translated (PtrTrans maps them to Drop)
//
// `qt_sut` runs one case from stdin. `qt_sut --batch` runs many cases separated by "=== id"
// lines, each under catch_unwind (used only for the mutation analysis).
use ::quadtree::*;
use std::any::Any;
use std::cell::{Cell, RefCell};
use std::fmt::Write as _;
use std::io::{self, Read, Write};

thread_local! {
    static DROPS: RefCell<Vec<i64>> = RefCell::new(Vec::new());
}

pub struct KeyBox(pub i64);
impl Drop for KeyBox {
    fn drop(&mut self) {
        DROPS.with(|d| d.borrow_mut().push(self.0));
    }
}

fn fx(s: &str) -> f64 {
    f64::from_bits(u64::from_str_radix(s, 16).expect("hex float"))
}
fn pf(o: &mut String, d: f64) {
    let _ = write!(o, " f{:016x}", d.to_bits());
}
fn pbounds(o: &mut String, b: Option<&QuadtreeBounds>) {
    match b {
        None => o.push_str(" nobounds"),
        Some(b) => {
            let nw = b.nw.as_deref().expect("bounds.nw");
            let se = b.se.as_deref().expect("bounds.se");
            pf(o, nw.x);
            pf(o, nw.y);
            pf(o, se.x);
            pf(o, se.y);
            pf(o, b.width);
            pf(o, b.height);
        }
    }
}
fn ppreds(o: &mut String, n: &QuadtreeNode) {
    let _ = write!(
        o,
        " {}{}{}",
        quadtree_node_isleaf(Some(n)) as i32,
        quadtree_node_isempty(Some(n)) as i32,
        quadtree_node_ispointer(Some(n)) as i32
    );
}
fn key_of(id: i64) -> Option<Box<dyn Any>> {
    if id < 0 {
        None
    } else {
        Some(Box::new(KeyBox(id)) as Box<dyn Any>)
    }
}
fn key_id(k: &Option<Box<dyn Any>>) -> i64 {
    match k {
        None => -1,
        Some(b) => b.downcast_ref::<KeyBox>().map(|kb| kb.0).unwrap_or(-2),
    }
}

fn run_case(script: &str) -> String {
    let out = RefCell::new(String::new());
    DROPS.with(|d| d.borrow_mut().clear());
    // key_free logger; declared before the tree so it outlives it.
    let kf_log = RefCell::new(Vec::<i64>::new());
    let kf = |k: &dyn Any| {
        kf_log
            .borrow_mut()
            .push(k.downcast_ref::<KeyBox>().map(|kb| kb.0).unwrap_or(-1));
    };
    let mut key_free_set = false;
    let mut tree: Option<Box<Quadtree>> = None;
    let mut bounds: Option<Box<QuadtreeBounds>> = None;
    for line in script.lines() {
        let t: Vec<&str> = line.split_whitespace().collect();
        if t.is_empty() || t[0].starts_with('#') {
            continue;
        }
        let mut o = String::new();
        match t[0] {
            "N" | "n" => {
                tree = quadtree_new(fx(t[1]), fx(t[2]), fx(t[3]), fx(t[4]));
                if t[0] == "N" {
                    match tree.as_deref() {
                        None => o.push_str("N null\n"),
                        Some(tr) => {
                            o.push('N');
                            pbounds(&mut o, tr.root.as_deref().and_then(|r| r.bounds.as_deref()));
                            let _ = writeln!(o, " L{}", tr.length);
                        }
                    }
                }
            }
            "I" | "i" => {
                let tr = tree.as_deref_mut();
                let r = quadtree_insert(tr, fx(t[1]), fx(t[2]), key_of(t[3].parse().unwrap()));
                if t[0] == "I" {
                    let _ = writeln!(o, "I {} L{}", r, tree.as_deref().unwrap().length);
                }
            }
            "S" => match quadtree_search(tree.as_deref(), fx(t[1]), fx(t[2])) {
                Some(p) => {
                    o.push_str("S hit");
                    pf(&mut o, p.x);
                    pf(&mut o, p.y);
                    o.push('\n');
                }
                None => o.push_str("S null\n"),
            },
            "W" => {
                let depth = Cell::new(0i32);
                let buf = RefCell::new(String::new());
                let d = |n: &QuadtreeNode| {
                    let mut s = String::new();
                    let _ = write!(s, "D {}", depth.get());
                    ppreds(&mut s, n);
                    pbounds(&mut s, n.bounds.as_deref());
                    match n.point.as_deref() {
                        Some(p) => {
                            s.push_str(" P");
                            pf(&mut s, p.x);
                            pf(&mut s, p.y);
                        }
                        None => s.push_str(" P-"),
                    }
                    let _ = writeln!(s, " K{}", key_id(&n.key));
                    buf.borrow_mut().push_str(&s);
                    depth.set(depth.get() + 1);
                };
                let a = |_n: &QuadtreeNode| {
                    depth.set(depth.get() - 1);
                    let _ = writeln!(buf.borrow_mut(), "A {}", depth.get());
                };
                quadtree_walk(
                    tree.as_deref().and_then(|t| t.root.as_deref()),
                    Some(&d as &dyn Fn(&QuadtreeNode)),
                    Some(&a as &dyn Fn(&QuadtreeNode)),
                );
                o.push_str(&buf.into_inner());
            }
            "L" => {
                let _ = writeln!(o, "L {}", tree.as_deref().unwrap().length);
            }
            "K" => {
                if let Some(tr) = tree.as_deref_mut() {
                    tr.key_free = Some(&kf as &dyn FnMut(&dyn Any));
                    key_free_set = true;
                }
            }
            "B" => {
                bounds = quadtree_bounds_new();
                o.push('B');
                pbounds(&mut o, bounds.as_deref());
                o.push('\n');
            }
            "BX" => {
                quadtree_bounds_extend(bounds.as_deref_mut(), fx(t[1]), fx(t[2]));
                o.push_str("BX");
                pbounds(&mut o, bounds.as_deref());
                o.push('\n');
            }
            "NB" => {
                let n = quadtree_node_with_bounds(fx(t[1]), fx(t[2]), fx(t[3]), fx(t[4])).unwrap();
                o.push_str("NB");
                ppreds(&mut o, &n);
                pbounds(&mut o, n.bounds.as_deref());
                o.push('\n');
            }
            "NN" => {
                let n = quadtree_node_new().unwrap();
                o.push_str("NN");
                ppreds(&mut o, &n);
                pbounds(&mut o, n.bounds.as_deref());
                o.push('\n');
            }
            "PN" => {
                let p = quadtree_point_new(fx(t[1]), fx(t[2])).unwrap();
                o.push_str("PN");
                pf(&mut o, p.x);
                pf(&mut o, p.y);
                o.push('\n');
            }
            other => {
                let _ = writeln!(o, "? {}", other);
            }
        }
        // key_free calls made by the translation during this command, in the C's format
        for k in kf_log.borrow_mut().drain(..) {
            let _ = writeln!(o, "KF {}", k);
        }
        // Informational ('#' lines are not compared): keys released through Drop during this command
        if key_free_set {
            let d: Vec<i64> = DROPS.with(|d| d.borrow_mut().drain(..).collect());
            if !d.is_empty() {
                let _ = writeln!(o, "# rust_key_drops_in_this_command {:?}", d);
            }
        }
        out.borrow_mut().push_str(&o);
        if !BATCH.with(|b| b.get()) {
            let mut so = io::stdout().lock();
            let _ = so.write_all(o.as_bytes());
            let _ = so.flush();
            out.borrow_mut().clear();
        }
    }
    drop(tree);
    // Informational only ('#' lines are ignored by the comparison): keys the translation released
    // through Drop, in order, when key_free was set (the C releases them through key_free).
    if key_free_set {
        let d = DROPS.with(|d| d.borrow().clone());
        let line = format!("# rust_key_drops_at_teardown {:?}\n", d);
        if BATCH.with(|b| b.get()) {
            out.borrow_mut().push_str(&line);
        } else {
            let _ = io::stdout().write_all(line.as_bytes());
        }
    }
    out.into_inner()
}

thread_local! {
    static BATCH: Cell<bool> = Cell::new(false);
}

fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    let batch = std::env::args().nth(1).as_deref() == Some("--batch");
    if !batch {
        run_case(&input);
        return;
    }
    BATCH.with(|b| b.set(true));
    std::panic::set_hook(Box::new(|_| {}));
    let mut cases: Vec<(String, String)> = Vec::new();
    for line in input.lines() {
        if let Some(id) = line.strip_prefix("=== ") {
            cases.push((id.to_string(), String::new()));
        } else if let Some(c) = cases.last_mut() {
            c.1.push_str(line);
            c.1.push('\n');
        }
    }
    let stdout = io::stdout();
    for (id, script) in cases {
        let r = std::panic::catch_unwind(|| run_case(&script));
        let mut so = stdout.lock();
        let _ = writeln!(so, "=== {}", id);
        match r {
            Ok(s) => {
                let _ = so.write_all(s.as_bytes());
            }
            Err(_) => {
                let _ = writeln!(so, "PANIC");
            }
        }
        let _ = so.flush();
    }
}
