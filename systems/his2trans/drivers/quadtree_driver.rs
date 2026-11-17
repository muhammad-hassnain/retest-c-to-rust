// quadtree: the translation keeps the C-ABI raw-pointer API (extern "C", *mut quadtree_t) but the
// structs are Rust-native (Option<Box<..>> children/bounds/point, not repr(C)); the driver reads
// those pub fields directly (tree.length, node.bounds, node.point, node.key).
use crate::src_quadtree::*;
use crate::src_node::*;
use crate::src_bounds::*;
use crate::src_point::*;
use crate::types::{quadtree_t, quadtree_node_t, quadtree_bounds_t, quadtree_point_t};
static mut TREE: *mut quadtree_t = std::ptr::null_mut();
fn pbounds(b: Option<&quadtree_bounds_t>) {
    match b {
        None => print!(" nob"),
        Some(b) => { pd(b.nw.x); pd(b.nw.y); pd(b.se.x); pd(b.se.y); pd(b.width); pd(b.height); }
    }
}
unsafe extern "C" fn descent(n: *mut quadtree_node_t) {
    print!(" D");
    pi(quadtree_node_isleaf(n) as i64); pi(quadtree_node_isempty(n) as i64); pi(quadtree_node_ispointer(n) as i64);
    let r = &*n;
    pbounds(r.bounds.as_deref());
    match r.point.as_deref() { Some(p) => { pd(p.x); pd(p.y); } None => print!(" -") }
    pu(r.key as usize as u64);
}
unsafe extern "C" fn ascent(_n: *mut quadtree_node_t) { print!(" A"); }
unsafe extern "C" fn nofree(_k: *mut crate::types::c_void) {}
fn dispatch(tok: &[&str]) {
    let op = tok[0];
    unsafe {
        match op {
            "new" => {
                TREE = quadtree_new(rg_dbl(tok[1]), rg_dbl(tok[2]), rg_dbl(tok[3]), rg_dbl(tok[4]));
                if TREE.is_null() { print!(" NULL"); return; }
                let t = &mut *TREE;
                pu(t.length as u64);
                let root: *mut quadtree_node_t = match t.root.as_deref_mut() { Some(r) => r, None => std::ptr::null_mut() };
                quadtree_walk(root, Some(descent), Some(ascent));
            }
            "ins" => {
                let r = quadtree_insert(TREE, rg_dbl(tok[1]), rg_dbl(tok[2]), rg_int(tok[3]) as usize as *mut crate::types::c_void);
                pi(r as i64);
                pu((*TREE).length as u64);
            }
            "srch" => {
                let p = quadtree_search(TREE, rg_dbl(tok[1]), rg_dbl(tok[2]));
                if p.is_null() { print!(" NULL"); } else { pd((*p).x); pd((*p).y); }
            }
            "walk" => {
                let t = &mut *TREE;
                pu(t.length as u64);
                let root: *mut quadtree_node_t = match t.root.as_deref_mut() { Some(r) => r, None => std::ptr::null_mut() };
                quadtree_walk(root, Some(descent), Some(ascent));
            }
            "bounds" => {
                let b = quadtree_bounds_new();
                let mut i = 1;
                while i + 1 < tok.len() { quadtree_bounds_extend(b, rg_dbl(tok[i]), rg_dbl(tok[i + 1])); i += 2; }
                pbounds(Some(&*b));
                quadtree_bounds_free(b);
            }
            "nwb" => {
                let n = quadtree_node_with_bounds(rg_dbl(tok[1]), rg_dbl(tok[2]), rg_dbl(tok[3]), rg_dbl(tok[4]));
                descent(n);
                quadtree_node_free(n, Some(nofree));
            }
            "pnew" => {
                let p = quadtree_point_new(rg_dbl(tok[1]), rg_dbl(tok[2]));
                pd((*p).x); pd((*p).y);
                quadtree_point_free(p);
            }
            _ => {}
        }
    }
}
