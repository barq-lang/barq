use std::os::unix::fs::symlink;
use std::path::Path;

fn main() {
    let out = std::env::var("OUT_DIR").unwrap();
    let sym = format!("{}/libsqlite3.so", out);
    if !Path::new(&sym).exists() {
        // Try common locations for the versioned shared library
        let candidates = [
            "/usr/lib/x86_64-linux-gnu/libsqlite3.so.0",
            "/usr/lib/aarch64-linux-gnu/libsqlite3.so.0",
            "/usr/lib/libsqlite3.so.0",
            "/usr/local/lib/libsqlite3.so.0",
        ];
        for src in &candidates {
            if Path::new(src).exists() {
                let _ = symlink(src, &sym);
                break;
            }
        }
    }
    println!("cargo:rustc-link-search=native={}", out);
    println!("cargo:rustc-link-lib=dylib=sqlite3");
}
