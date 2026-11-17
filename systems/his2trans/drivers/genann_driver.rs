// genann: the translation's API is genann_init(..) -> Option<Box<genann>> (Vec fields),
// genann_run(&mut genann, &[f64]) -> &[f64], genann_train(&mut genann, &[f64], &[f64], f64),
// genann_copy(&genann) -> Box<genann>, genann_randomize(&mut genann), genann_read/write(*mut FILE).
// FILE* is the process's libc stdout / a libc tmpfile(), cast to the crate's opaque FILE type.
use crate::src_genann::*;
use crate::types::genann;
extern "C" {
    static mut stdout: *mut libc::FILE;
}
fn act_of(c: i64) -> crate::types::genann_actfun {
    match c {
        1 => genann_act_sigmoid,
        2 => genann_act_threshold,
        3 => genann_act_linear,
        _ => genann_act_sigmoid_cached,
    }
}
fn W(k: i32, scale: f64) -> f64 {
    ((k * 37) % 101 - 50) as f64 / 100.0 * scale
}
fn dump(a: Option<&genann>) {
    match a {
        None => print!(" NULL"),
        Some(a) => {
            pi(a.inputs as i64); pi(a.hidden_layers as i64); pi(a.hidden as i64); pi(a.outputs as i64);
            pi(a.total_weights as i64); pi(a.total_neurons as i64);
            print!(" w");
            for i in 0..a.total_weights as usize { pd(a.weight[i]); }
        }
    }
}
fn dispatch(tok: &[&str]) {
    let op = tok[0];
    match op {
        "sig" => { pd(genann_act_sigmoid(rg_dbl(tok[1]))); return; }
        "sigc" => { pd(genann_act_sigmoid_cached(rg_dbl(tok[1]))); return; }
        "thr" => { pd(genann_act_threshold(rg_dbl(tok[1]))); return; }
        "lin" => { pd(genann_act_linear(rg_dbl(tok[1]))); return; }
        _ => {}
    }
    if op == "seed" {
        unsafe { libc::srand(rg_int(tok[1]) as libc::c_uint); }
        return;
    }
    if op == "read" {
        let bytes = rg_hex(tok[1]);
        unsafe {
            let f = libc::tmpfile();
            libc::fwrite(bytes.as_ptr() as *const libc::c_void, 1, bytes.len(), f);
            libc::rewind(f);
            io::stdout().flush().unwrap();
            let a = genann_read(f as *mut crate::types::FILE);
            libc::fflush(std::ptr::null_mut());
            dump(a.as_deref());
            libc::fclose(f);
        }
        return;
    }
    let (i, hl, h, o) = (rg_int(tok[1]) as i32, rg_int(tok[2]) as i32, rg_int(tok[3]) as i32, rg_int(tok[4]) as i32);
    let a = genann_init(i, hl, h, o);
    if op == "init" { dump(a.as_deref()); return; }
    let mut a = match a { None => { print!(" NULL"); return; } Some(a) => a };
    if op == "randz" {
        for k in 0..a.total_weights as usize { a.weight[k] = 0.0; }
        genann_randomize(&mut a);
        dump(Some(&a));
    } else if op == "run" || op == "train" {
        let (acth, acto) = (rg_int(tok[5]), rg_int(tok[6]));
        if tok[7] != "r" {
            let scale = rg_dbl(tok[7]);
            for k in 0..a.total_weights { a.weight[k as usize] = W(k, scale); }
        }
        a.activation_hidden = act_of(acth);
        a.activation_output = act_of(acto);
        if op == "run" {
            let x: Vec<f64> = (0..i as usize).map(|k| rg_dbl(tok[8 + k])).collect();
            let out = genann_run(&mut a, &x).to_vec();
            print!(" out");
            for k in 0..o as usize { pd(out[k]); }
            print!(" neurons");
            for k in 0..a.total_neurons as usize { pd(a.output[k]); }
        } else {
            let lr = rg_dbl(tok[8]);
            let reps = rg_int(tok[9]);
            let x: Vec<f64> = (0..i as usize).map(|k| rg_dbl(tok[10 + k])).collect();
            let t: Vec<f64> = (0..o as usize).map(|k| rg_dbl(tok[10 + i as usize + k])).collect();
            for _ in 0..reps { genann_train(&mut a, &x, &t, lr); }
            dump(Some(&a));
            let out = genann_run(&mut a, &x).to_vec();
            print!(" out");
            for k in 0..o as usize { pd(out[k]); }
        }
    } else if op == "copy" {
        let scale = rg_dbl(tok[5]);
        for k in 0..a.total_weights { a.weight[k as usize] = W(k, scale); }
        let c = genann_copy(&a);
        for k in 0..a.total_weights as usize { a.weight[k] = -1.0; }
        dump(Some(&c));
    } else if op == "write" {
        let scale = rg_dbl(tok[5]);
        for k in 0..a.total_weights { a.weight[k as usize] = W(k, scale); }
        print!(" ");
        io::stdout().flush().unwrap();
        unsafe {
            genann_write(&a, stdout as *mut crate::types::FILE);
            libc::fflush(stdout);
        }
    }
}
