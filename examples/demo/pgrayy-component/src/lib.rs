wit_bindgen::generate!({
    world: "pgrayy-client",
    path: "wit/pgrayy.wit",
});

use wasi_http_client::Client;

struct PgrayyClient;

impl Guest for PgrayyClient {
    fn greet(name: String) -> String {
        format!("Hello, {}! Greetings from a WASM component.", name)
    }

    fn add(a: i32, b: i32) -> i32 {
        a + b
    }

    async fn fetch(url: String) -> String {
        let response = Client::new()
            .get(&url)
            .send();
        match response {
            Ok(resp) => {
                let status = resp.status();
                let body = resp.body().unwrap_or_default();
                format!("HTTP {} | {}", status, String::from_utf8_lossy(&body))
            }
            Err(e) => format!("error: {e}"),
        }
    }
}

export!(PgrayyClient);
