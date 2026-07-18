from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room, emit
import random, string
import os
import requests, json
api_key = ""
MODEL = "llama-3.1-8b-instant"
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

rooms = {}
user_sessions = {}
FILES_DIR = "files"
os.makedirs(FILES_DIR, exist_ok=True)
for fname in os.listdir(FILES_DIR):
    if fname.endswith(".txt"):
        room = fname.replace(".txt","")
        with open(os.path.join(FILES_DIR, fname), "r", encoding="utf-8") as f:
            rooms[room] = {"text": f.read(), "users": {}, "password":"", "docname": fname}

def gen_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        username = request.form["username"]
        emoji = request.form.get("emoji", "")
        color = request.form["color"]
        password = request.form["password"]
        docname = request.form["docname"]
        room = request.form["room"]
        file_text = request.form.get("fileText", "")

        if not room:
            room = gen_code()
            rooms[room] = {
                "text": file_text,
                "users": {},
                "password": password,
                "docname": docname
            }
        else:
            if room not in rooms or rooms[room]["password"] != password:
                return "Invalid room or password"

        return redirect(url_for("editor", room=room, name=username, color=color, emoji=emoji))

    return render_template("home.html")
def groq_doc_intent(user_text, doc_text, api_key):
    system_prompt = """
You are an AI inside a document editor.

Decide intent and return ONLY valid JSON.

Format EXACTLY like this:
{
  "action": "replace" | "none",
  "content": "text"
}

Rules:
- If the user asks to WRITE, EDIT, SUMMARIZE, REPLACE, INSERT, CONTINUE, or MODIFY the document → action = "replace"
- If the user is just chatting, asking opinions, or questions → action = "none"
- content MUST be plain text
- NO markdown
- NO explanations
- NO extra keys
"""

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"User request:\n{user_text}\n\nCurrent document:\n{doc_text}"
            }
        ],
        "temperature": 0.2
    }

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=20
        )

        if r.status_code != 200:
            print("Groq HTTP Error:", r.status_code, r.text)
            return {"action": "none", "content": "AI request failed"}

        res = r.json()

        if "choices" not in res or not res["choices"]:
            print("Groq Invalid Response:", res)
            return {"action": "none", "content": "AI returned invalid response"}

        raw = res["choices"][0]["message"]["content"].strip()

        # 1️⃣ Try strict JSON parse
        try:
            parsed = json.loads(raw)
            return {
                "action": parsed.get("action", "none"),
                "content": parsed.get("content", "")
            }
        except json.JSONDecodeError:
            pass

        # 2️⃣ Extract JSON from messy output
        match = re.search(r"\{[\s\S]*?\}", raw)
        if match:
            try:
                parsed = json.loads(match.group())
                return {
                    "action": parsed.get("action", "none"),
                    "content": parsed.get("content", "")
                }
            except Exception:
                pass

        # 3️⃣ Last fallback → treat as chat
        return {
            "action": "none",
            "content": raw
        }

    except Exception as e:
        print("Groq Exception:", e)
        return {"action": "none", "content": "AI request failed"}

@socketio.on("ai_prompt")
def ai_prompt(data):
    room = data.get("room")
    prompt = data.get("prompt", "")
    text = data.get("text", "")
    api_key = data.get("api_key", "")

    # 🔒 Safety check
    if not api_key:
        emit("ai_response", {
            "action": "none",
            "content": "Groq API key missing"
        }, room=request.sid)
        return

    # ✅ PASS api_key
    result = groq_doc_intent(prompt, text, api_key)

    # Send AI reply to AI panel (only requester)
    emit("ai_response", {
        "action": result.get("action", "none"),
        "content": result.get("content", "")
    }, room=request.sid)

    # 🧠 If AI modified document → sync to everyone
    if result.get("action") == "replace" and room in rooms:
        new_text = result.get("content", "")

        # server source of truth
        rooms[room]["text"] = new_text

        # broadcast like a real edit
        socketio.emit(
            "text_sync",
            new_text,
            room=room
        )

@app.route("/doc/<room>")
def editor(room):
    name = request.args.get("name")
    color = request.args.get("color")
    emoji = request.args.get("emoji")

    return render_template(
        "editor.html",
        room=room,
        name=name,
        color=color,
        emoji=emoji,
        docname=rooms[room]["docname"]
    )


@socketio.on("join")
def join(data):
    room = data["room"]
    name = data["name"]
    color = data["color"]
    emoji = data["emoji"]

    if name in rooms[room]["users"]:
        emit("join_error", "Name already taken", room=request.sid)
        return

    for u in rooms[room]["users"].values():
        if u["color"] == color:
            emit("join_error", "Color already taken", room=request.sid)
            return

    join_room(room)

    rooms[room]["users"][name] = {
        "color": color,
        "emoji": emoji
    }

    user_sessions[request.sid] = (room, name)

    emit("user_list", rooms[room]["users"], room=room)
    emit("popup", f"{name} joined the room", room=room)
    emit("load_text", rooms[room]["text"], room=request.sid)


@socketio.on("text_update")
def update_text(data):
    room = data["room"]
    rooms[room]["text"] = data["text"]
    emit("text_sync", data["text"], room=room, include_self=False)


@socketio.on("cursor_move")
def cursor_move(data):
    emit("cursor_sync", data, room=data["room"], include_self=False)


@socketio.on("chat_message")
def chat(data):
    emit("chat_sync", data, room=data["room"])


@socketio.on("leave")
def leave(data):
    room = data["room"]
    name = data["name"]

    leave_room(room)
    rooms[room]["users"].pop(name, None)

    emit("user_list", rooms[room]["users"], room=room)
    emit("popup", f"{name} left the room", room=room)
    emit("user_left", name, room=room)


@socketio.on("disconnect")
def disconnect():
    if request.sid in user_sessions:
        room, name = user_sessions.pop(request.sid)
        rooms[room]["users"].pop(name, None)

        emit("user_list", rooms[room]["users"], room=room)
        emit("popup", f"{name} disconnected", room=room)
        emit("user_left", name, room=room)

def get_room_file(room):
    filename = f"{room}.txt"
    return os.path.join(FILES_DIR, filename)

@socketio.on("autosave")
def autosave(data):
    room = data["room"]
    text = data["text"]
    

    if room in rooms:
        rooms[room]["text"] = text
    
    filepath = get_room_file(room)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(text)
    
    emit("popup", "Autosaved!", room=request.sid)
@socketio.on("delete_file")
def delete_file(data):
    room = data["room"]
    
    if room in rooms:
        rooms.pop(room)
    
    filepath = get_room_file(room)
    if os.path.exists(filepath):
        os.remove(filepath)
    
    emit("popup", "File deleted!", room=request.sid)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
