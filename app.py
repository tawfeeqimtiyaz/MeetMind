from flask import Flask, request, jsonify
from flask_cors import CORS

import sqlite3
import os
import speech_recognition as sr
from pydub import AudioSegment


# ==============================
# BASIC APP SETUP
# ==============================

app = Flask(__name__)
CORS(app)
def init_db():
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            agenda TEXT,
            datetime TEXT,
            participants TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()



DATABASE = "database.db"
UPLOAD_FOLDER = "uploads"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==============================
# DATABASE CONNECTION
# ==============================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# ==============================
# DATABASE INITIALIZATION
# ==============================

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # ---- MEETINGS TABLE ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            agenda TEXT,
            datetime TEXT,
            participants TEXT
        )
    """)

    # ---- TRANSCRIPTS TABLE (STEP 6.3) ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER,
            transcript TEXT
        )
    """)

    conn.commit()
    conn.close()


# Initialize DB when app starts
init_db()

# ==============================
# HOME ROUTE (OPTIONAL BUT HELPFUL)
# ==============================

@app.route("/")
def home():
    return "SyncNotes backend is running 🚀"


# ==============================
# CREATE MEETING API
# ==============================

@app.route("/create-meeting", methods=["POST"])
def create_meeting():
    data = request.json

    title = data.get("title")
    agenda = data.get("agenda")
    datetime = data.get("datetime")
    participants = data.get("participants")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO meetings (title, agenda, datetime, participants)
        VALUES (?, ?, ?, ?)
    """, (title, agenda, datetime, participants))

    conn.commit()
    conn.close()

    return jsonify({"message": "Meeting created successfully"}), 201


# ==============================
# GET ALL MEETINGS
# ==============================

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Missing credentials"}), 400

    # TEMP ROLE LOGIC (for now)
    if email.endswith("@admin.com"):
        role = "admin"
    else:
        role = "member"

    return jsonify({"role": role})


# ==============================
# TRANSCRIPTION LOGIC
# ==============================

def transcribe_audio(file_path):
    recognizer = sr.Recognizer()

    with sr.AudioFile(file_path) as source:
        audio = recognizer.record(source)

    try:
        text = recognizer.recognize_google(audio)
        return text
    except:
        return "Transcription failed"


# ==============================
# UPLOAD RECORDING + TRANSCRIBE
# ==============================

@app.route("/upload-recording/<int:meeting_id>", methods=["POST"])
def upload_recording(meeting_id):

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]

    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # Convert to WAV if needed
    if not file.filename.lower().endswith(".wav"):
        audio = AudioSegment.from_file(file_path)
        wav_path = file_path.rsplit(".", 1)[0] + ".wav"
        audio.export(wav_path, format="wav")
        file_path = wav_path

    transcript = transcribe_audio(file_path)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO transcripts (meeting_id, transcript)
        VALUES (?, ?)
    """, (meeting_id, transcript))

    conn.commit()
    conn.close()

    return jsonify({
        "message": "Recording uploaded and transcribed",
        "transcript": transcript
    })


# ==============================
# RUN APP
# ==============================
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
@app.route("/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Missing credentials"}), 400

    # TEMP logic (replace later with real auth)
    if email.endswith("@admin.com"):
        role = "admin"
    else:
        role = "member"

    return jsonify({"role": role})
@app.route("/meetings", methods=["POST"])
def create_meeting():
    data = request.json

    title = data.get("title")
    agenda = data.get("agenda")
    datetime_val = data.get("datetime")
    participants = data.get("participants")

    if not title or not datetime_val:
        return jsonify({"error": "Missing required fields"}), 400

    conn = sqlite3.connect("database.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meetings (title, agenda, datetime, participants) VALUES (?, ?, ?, ?)",
        (title, agenda, datetime_val, participants)
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "meeting_created"})
@app.route("/meetings", methods=["GET"])
def get_meetings():
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    cur.execute("SELECT id, title, datetime FROM meetings ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    meetings = []
    for row in rows:
        meetings.append({
            "id": row[0],
            "title": row[1],
            "datetime": row[2]
        })

    return jsonify(meetings)

if __name__ == "__main__":
    app.run(debug=True)
