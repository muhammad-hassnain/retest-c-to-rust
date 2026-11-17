// rgba: the translation's API is rgba_from_string(&[u8]) -> Option<u32>, rgba_new(u32) -> rgba_t,
// rgba_to_string(rgba_t) -> String (no buffer/len), rgba_inspect(u32).
// Mapping of the C (ok, return) pair: Some(v) -> ok=1, v ; None -> ok=0, 0 (the C returns 0 when ok=0).
use crate::src_rgba::*;
use crate::types::rgba_t;
fn dispatch(tok: &[&str]) {
    let op = tok[0];
    if op == "fs" {
        let b = rg_hex(tok[1]);
        match rgba_from_string(upto_nul(&b)) {
            Some(v) => { pi(1); pu(v as u64); pu(v as u64); }
            None => { pi(0); pu(0); pu(0); }
        }
    } else if op == "new" {
        let c = rgba_new(tok[1].parse::<u64>().unwrap() as u32);
        pd(c.r); pd(c.g); pd(c.b); pd(c.a);
    } else if op == "ts" {
        let c = rgba_t { r: rg_dbl(tok[1]), g: rg_dbl(tok[2]), b: rg_dbl(tok[3]), a: rg_dbl(tok[4]) };
        let s = rgba_to_string(c);
        ps(Some(s.as_bytes()));
    } else if op == "insp" {
        print!(" ");
        rgba_inspect(tok[1].parse::<u64>().unwrap() as u32);
    }
}
