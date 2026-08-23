from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return """
    <html>
      <head><title>Flask Smoke Test</title></head>
      <body style="font-family: Arial; padding: 40px;">
        <h1>Flask is working</h1>
        <p>If you can see this page, the web interface can launch independently of the model.</p>
        <p>Health endpoint: <a href="/api/health">/api/health</a></p>
      </body>
    </html>
    """

@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "message": "Flask is running independently"
    })

@app.route("/api/scan", methods=["POST"])
def predict():
    return jsonify({
        "status": "ok",
        "message": "Dummy scan endpoint reached"
    })

if __name__ == "__main__":
    print("Starting smoke test on http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=True)