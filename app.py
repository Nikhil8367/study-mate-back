import os
import fitz  # PyMuPDF
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
import google.generativeai as genai
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from bson.objectid import ObjectId
import time

# === Load Environment Variables ===
load_dotenv()

app = Flask(__name__)
CORS(app)

# === MongoDB Setup ===
client = MongoClient(os.getenv("MONGO_URI"))
db = client["pdf_db"]
users_col = db["users"]
paragraphs_col = db["paragraphs"]
chats_col = db["chats"]

# === Gemini API Setup ===
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-2.0-flash")

# === Helper: Extract paragraphs from PDF ===
def extract_paragraphs_from_pdf(file_bytes):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    paragraphs = []
    for page in doc:
        text = page.get_text()
        paras = [p.strip() for p in text.split('\n\n') if p.strip()]
        paragraphs.extend(paras)
    return paragraphs

# === Route: Upload PDF (store paras per user) ===
@app.route("/upload", methods=["POST"])
def upload_pdf():
    username = request.form.get("username")
    if not username:
        return jsonify({"error": "Username required"}), 400

    # Clear previous paragraphs for this user
    paragraphs_col.delete_many({"username": username})

    if "files" not in request.files:
        return jsonify({"error": "No files uploaded"}), 400

    for file in request.files.getlist("files"):
        paragraphs = extract_paragraphs_from_pdf(file.read())
        for i, para in enumerate(paragraphs):
            paragraphs_col.insert_one({
                "username": username,
                "index": i,
                "text": para
            })

    return jsonify({"message": "PDF uploaded and paragraphs stored successfully."}), 200

# === Fetch Chat History (both PDF + Gemini) ===
@app.route("/history/<username>", methods=["GET"])
def get_history(username):
    try:
        # Fetch PDF-based chat history
        pdf_chats = list(db["chats"].find({"username": username}))
        for chat in pdf_chats:
            chat["_id"] = str(chat["_id"])
            chat["source"] = "pdf"

        # Fetch Gemini-based chat history
        gemini_chats = list(db["gemini_chats"].find({"username": username}))
        for chat in gemini_chats:
            chat["_id"] = str(chat["_id"])
            chat["source"] = "gemini"

        # Combine & sort by timestamp if present
        all_chats = pdf_chats + gemini_chats
        all_chats = sorted(all_chats, key=lambda x: x.get("timestamp", 0), reverse=True)

        return jsonify(all_chats), 200

    except Exception as e:
        return jsonify({"error": f"History Fetch Error: {str(e)}"}), 500

# === Gemini Chat Route (saved separately) ===
@app.route("/gemini_chat", methods=["POST"])
def gemini_chat():
    data = request.get_json()
    message = data.get("message", "").strip()
    username = data.get("username", "").strip()

    if not message or not username:
        return jsonify({"error": "Message and username are required"}), 400

    try:
        response = model.generate_content(message)
        reply = response.text

        # Save chat
        db["gemini_chats"].insert_one({
            "username": username,
            "question": message,
            "answer": reply,
            "timestamp": time.time()
        })

        return jsonify({"response": reply}), 200

    except Exception as e:
        return jsonify({"error": f"Gemini API Error: {str(e)}"}), 500

# === Ask Question from Uploaded PDF ===
@app.route("/ask", methods=["POST"])
def ask_question():
    data = request.get_json()
    question = data.get("question", "").strip()
    username = data.get("username", "").strip()

    if not question or not username:
        return jsonify({"error": "Question and username are required"}), 400

    user_paras = list(paragraphs_col.find({"username": username}))
    if not user_paras:
        return jsonify({"error": "No content found for this user"}), 404

    all_paragraphs = [doc["text"] for doc in user_paras]

    # Simple keyword scoring
    keywords = question.lower().split()
    scored = []
    for para in all_paragraphs:
        score = sum(word in para.lower() for word in keywords)
        if score > 0:
            scored.append((para, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_paragraphs = [p[0] for p in scored[:3]] if scored else all_paragraphs[:3]

    context = "\n\n".join(top_paragraphs)
    prompt = f"""Answer the question using only the following context. Do not use external knowledge.

Context:
{context}

Question: {question}"""

    try:
        response = model.generate_content(prompt)
        answer = response.text
    except Exception as e:
        return jsonify({"error": f"Gemini API Error: {str(e)}"}), 500

    # Save chat history
    chats_col.insert_one({
        "username": username,
        "question": question,
        "answer": answer,
        "matched_paragraphs": top_paragraphs,
        "timestamp": time.time()
    })

    return jsonify({
        "answer": answer,
        "matched_paragraphs": top_paragraphs
    }), 200

# === Signup ===
@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    if users_col.find_one({"username": username}):
        return jsonify({"error": "User already exists"}), 409

    users_col.insert_one({
        "username": username,
        "password": generate_password_hash(password)
    })

    return jsonify({"message": "User registered successfully."}), 200

# === Login ===
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    user = users_col.find_one({"username": username})
    if user and check_password_hash(user["password"], password):
        return jsonify({"message": "Login successful."}), 200
    return jsonify({"error": "Invalid credentials"}), 401

# === Delete Chat by ObjectId ===
from bson import ObjectId

from bson.errors import InvalidId

@app.route("/history/<source>/<chat_id>", methods=["DELETE"])
def delete_history(source, chat_id):
    try:
        if source == "pdf":
            collection = db["chats"]
        elif source == "gemini":
            collection = db["gemini_chats"]
        else:
            return jsonify({"error": "Invalid source. Use 'pdf' or 'gemini'."}), 400

        # Try ObjectId deletion
        try:
            valid_id = ObjectId(chat_id)  # will raise if invalid
            result = collection.delete_one({"_id": valid_id})
            if result.deleted_count == 1:
                return jsonify({"message": f"{source.capitalize()} chat deleted successfully"}), 200
        except InvalidId:
            pass  # Not a valid ObjectId, fall back

        # Try index deletion
        if chat_id.isdigit():
            chat_index = int(chat_id)
            chats = list(collection.find().sort("_id", 1))
            if 0 <= chat_index < len(chats):
                target_id = chats[chat_index]["_id"]
                collection.delete_one({"_id": target_id})
                return jsonify({"message": f"{source.capitalize()} chat deleted successfully"}), 200
            else:
                return jsonify({"error": "Invalid chat index"}), 404

        return jsonify({"error": "Chat not found"}), 404

    except Exception as e:
        return jsonify({"error": f"Delete Error: {str(e)}"}), 500



# === Root Route ===
@app.route("/")
def home():
    return "✅ StudyMate Flask Backend is running!"

# === Run App ===
if __name__ == "__main__":
    app.run(debug=True)


