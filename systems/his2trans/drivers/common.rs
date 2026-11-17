// Shared helpers for the His2Trans Rust-side differential drivers (study-written, not from the
// artifact). Same line protocol as drivers/common.h. run.py concatenates this file with
// drivers/<project>_driver.rs into src/regrade_driver.rs of a scratch copy of the crate.
#![allow(dead_code, unused_imports, unused_variables, unused_mut, non_snake_case)]
use std::io::{self, BufRead, Write};

pub fn rg_hex(h: &str) -> Vec<u8> {
    if h == "~" {
        return Vec::new();
    }
    (0..h.len() / 2)
        .map(|i| u8::from_str_radix(&h[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

pub fn rg_dbl(h: &str) -> f64 {
    f64::from_bits(u64::from_str_radix(h, 16).unwrap())
}

pub fn rg_int(s: &str) -> i64 {
    s.parse::<i64>().unwrap()
}

pub fn pd(d: f64) {
    print!(" d:{:016x}", d.to_bits());
}

pub fn psn(s: &[u8]) {
    print!(" s:");
    if s.is_empty() {
        print!("~");
        return;
    }
    let mut o = String::with_capacity(2 * s.len());
    for b in s {
        o.push_str(&format!("{:02x}", b));
    }
    print!("{}", o);
}

/// bytes up to the first NUL (what C's "%s" / strlen would see)
pub fn upto_nul(s: &[u8]) -> &[u8] {
    match s.iter().position(|&c| c == 0) {
        Some(p) => &s[..p],
        None => s,
    }
}

pub fn ps(s: Option<&[u8]>) {
    match s {
        None => print!(" NULL"),
        Some(b) => psn(b),
    }
}

pub fn pi(v: i64) {
    print!(" {}", v);
}

pub fn pu(v: u64) {
    print!(" {}", v);
}

pub fn main() {
    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = line.unwrap();
        let tok: Vec<&str> = line.split_whitespace().collect();
        if tok.is_empty() {
            continue;
        }
        print!("{}", tok[0]);
        dispatch(&tok);
        println!();
        io::stdout().flush().unwrap();
    }
}
