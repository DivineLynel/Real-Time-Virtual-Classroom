import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, url_for
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
#real time event server
socketio = SocketIO(app, cors_allowed_origins="*")

room_states = {}
classroom_scoreboard = {}
quiz_answers = {}
connected_clients = {}

DB_FILE = Path(__file__).resolve().parent / 'classroom.db'


@contextmanager
def database_connection():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database():
    with database_connection() as connection:
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_index INTEGER NOT NULL CHECK (correct_index BETWEEN 0 AND 3)
            );

            CREATE TABLE IF NOT EXISTS scoreboard_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                room_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scoreboard_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_id INTEGER NOT NULL REFERENCES scoreboard_archives(id) ON DELETE CASCADE,
                student_name TEXT NOT NULL,
                score INTEGER NOT NULL
            );
        ''')
        question_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(questions)')
        }
        if 'room_code' not in question_columns:
            connection.execute(
                "ALTER TABLE questions ADD COLUMN room_code TEXT NOT NULL DEFAULT ''"
            )
        archive_columns = {
            row['name'] for row in connection.execute('PRAGMA table_info(scoreboard_archives)')
        }
        if 'room_code' not in archive_columns:
            connection.execute(
                "ALTER TABLE scoreboard_archives ADD COLUMN room_code TEXT NOT NULL DEFAULT ''"
            )


def load_questions(room_code):
    with database_connection() as connection:
        rows = connection.execute(
            '''SELECT id, question, options_json, correct_index FROM questions
               WHERE room_code = ? ORDER BY id''',
            (room_code,),
        ).fetchall()
    return [
        {
            'id': row['id'],
            'question': row['question'],
            'options': json.loads(row['options_json']),
            'correct_index': row['correct_index'],
        }
        for row in rows
    ]


def load_scoreboard_archives(room_code):
    with database_connection() as connection:
        return connection.execute(
            '''SELECT id, name, created_at FROM scoreboard_archives
               WHERE room_code = ? ORDER BY id DESC''',
            (room_code,),
        ).fetchall()


init_database()


def normalize_room_code(value):
    room_code = (value or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{1,30}', room_code):
        return None
    return room_code


def get_room_state(room_code):
    if room_code not in room_states:
        room_states[room_code] = {
            'active_question': None,
            'timer_seconds': 15,
            'timer_is_active': False,
            'quiz_has_started': False,
            'round_id': 0,
        }
    return room_states[room_code]


def run_background_countdown(room_code, round_id):
    state = get_room_state(room_code)
    while state['timer_seconds'] > 0:
        socketio.sleep(1)
        if not state['timer_is_active'] or state['round_id'] != round_id:
            return
        state['timer_seconds'] -= 1

        socketio.emit(
            'timer_countdown_tick',
            {"seconds_left": state['timer_seconds']},
            to=room_code,
        )

    state['timer_is_active'] = False
    active_question = state['active_question']
    correct_index = active_question['correct_index']
    room_answers = quiz_answers.get(room_code, {})
    for nickname in classroom_scoreboard.get(room_code, {}):
        chosen_index = room_answers.get(nickname)
        result = {
            'answered': nickname in room_answers,
            'chosen_index': chosen_index,
            'correct': chosen_index == correct_index,
            'selected_answer': (
                active_question['options'][chosen_index]
                if chosen_index is not None
                else None
            ),
            'correct_index': correct_index,
            'correct_answer': active_question['options'][correct_index],
        }
        for sid, client in connected_clients.items():
            if client['room_code'] == room_code and client['role'] == 'student' and client['nickname'] == nickname:
                socketio.emit('quiz_result', result, to=sid)
    socketio.emit('quiz_timer_expired', to=room_code)

@app.route('/')
def home_page():
    return render_template('welcome.html', error=None)

@app.route('/student')
def student_page():
    nickname = (request.args.get('nickname') or '').strip()
    room_code = normalize_room_code(request.args.get('room_code'))
    if not nickname or len(nickname) > 30 or not room_code:
        return render_template(
            'welcome.html',
            error='Enter a nickname (1–30 characters) and a classroom code (letters, numbers, _ or -).',
        ), 400
    return render_template('index.html', nickname=nickname, room_code=room_code)

@app.route('/professor')
def professor_page():
    room_code = normalize_room_code(request.args.get('room_code'))
    if not room_code:
        return render_template('welcome.html', error='Enter a valid classroom code to open the professor dashboard.'), 400
    return render_professor_page(
        room_code,
        archive_saved=request.args.get('archive_saved') == '1',
    )


def render_professor_page(room_code, error=None, form_data=None, selected_archive=None, archive_saved=False, status=200):
    state = get_room_state(room_code)
    archive_entries = []
    if selected_archive:
        with database_connection() as connection:
            archive_entries = connection.execute(
                '''SELECT student_name, score FROM scoreboard_entries
                   WHERE archive_id = ? ORDER BY score DESC, student_name''',
                (selected_archive['id'],),
            ).fetchall()
    return render_template(
        'professor.html',
        room_code=room_code,
        questions=load_questions(room_code),
        timer_is_active=state['timer_is_active'],
        error=error,
        form_data=form_data or {},
        scoreboard_archives=load_scoreboard_archives(room_code),
        selected_archive=selected_archive,
        archive_entries=archive_entries,
        archive_saved=archive_saved,
    ), status


@app.route('/professor/questions', methods=['POST'])
def create_question():
    room_code = normalize_room_code(request.form.get('room_code'))
    if not room_code:
        abort(400)
    question_text = request.form.get('question', '').strip()
    options = [request.form.get(f'option_{index}', '').strip() for index in range(4)]
    correct_index = request.form.get('correct_index', '')

    if not question_text or any(not option for option in options):
        error = 'Enter a question and all four answer choices.'
    elif correct_index not in {'0', '1', '2', '3'}:
        error = 'Select exactly one correct answer.'
    else:
        with database_connection() as connection:
            cursor = connection.execute(
                '''INSERT INTO questions (room_code, question, options_json, correct_index)
                   VALUES (?, ?, ?, ?)''',
                (room_code, question_text, json.dumps(options), int(correct_index)),
            )
        return redirect(url_for('professor_page', room_code=room_code))

    return render_professor_page(room_code, error=error, form_data=request.form, status=400)


@app.route('/professor/scoreboards', methods=['POST'])
def save_scoreboard():
    room_code = normalize_room_code(request.form.get('room_code'))
    if not room_code:
        abort(400)
    room_scoreboard = classroom_scoreboard.get(room_code, {})
    archive_name = request.form.get('archive_name', '').strip()
    if not archive_name:
        return render_professor_page(room_code, error='Enter a name for this scoreboard archive.', status=400)
    if len(archive_name) > 100:
        return render_professor_page(room_code, error='Archive names must be 100 characters or fewer.', status=400)
    if not room_scoreboard:
        return render_professor_page(room_code, error='There are no student scores to save yet.', status=400)

    with database_connection() as connection:
        cursor = connection.execute(
            'INSERT INTO scoreboard_archives (name, room_code) VALUES (?, ?)',
            (archive_name, room_code),
        )
        archive_id = cursor.lastrowid
        connection.executemany(
            'INSERT INTO scoreboard_entries (archive_id, student_name, score) VALUES (?, ?, ?)',
            [
                (archive_id, student['name'], student['score'])
                for student in room_scoreboard.values()
            ],
        )

    return redirect(url_for('professor_page', room_code=room_code, archive_saved=1))


@app.route('/professor/scoreboards/<int:archive_id>')
def view_scoreboard_archive(archive_id):
    room_code = normalize_room_code(request.args.get('room_code'))
    if not room_code:
        abort(400)
    with database_connection() as connection:
        archive = connection.execute(
            '''SELECT id, name, created_at FROM scoreboard_archives
               WHERE id = ? AND room_code = ?''',
            (archive_id, room_code),
        ).fetchone()
    if archive is None:
        abort(404)
    return render_professor_page(room_code, selected_archive=archive)


@app.route('/professor/questions/<int:question_id>/start', methods=['POST'])
def start_question(question_id):
    room_code = normalize_room_code(request.form.get('room_code'))
    if not room_code:
        abort(400)

    with database_connection() as connection:
        row = connection.execute(
            '''SELECT id, question, options_json, correct_index FROM questions
               WHERE id = ? AND room_code = ?''',
            (question_id, room_code),
        ).fetchone()
    if row is None:
        abort(404)
    question = {
        'id': row['id'],
        'question': row['question'],
        'options': json.loads(row['options_json']),
        'correct_index': row['correct_index'],
    }
    state = get_room_state(room_code)

    try:
        duration = int(request.form.get('seconds', '15'))
    except ValueError:
        duration = 0

    if not 5 <= duration <= 120:
        return render_professor_page(room_code,
            error='Choose a timer between 5 and 120 seconds.',
            status=400,
        )

    if state['timer_is_active']:
        return render_professor_page(room_code,
            error='Wait for the current question timer to finish before starting another.',
            status=409,
        )

    state['active_question'] = question
    state['quiz_has_started'] = True
    state['timer_seconds'] = duration
    state['timer_is_active'] = True
    state['round_id'] += 1
    round_id = state['round_id']
    quiz_answers[room_code] = {}

    socketio.emit('incoming_quiz_question', {
        'question': question['question'],
        'options': question['options'],
        'seconds': duration,
    }, to=room_code)
    socketio.emit('timer_countdown_tick', {'seconds_left': duration}, to=room_code)
    socketio.start_background_task(run_background_countdown, room_code, round_id)
    return redirect(url_for('professor_page', room_code=room_code))

@socketio.on('connect')
def handle_user_connection(auth=None):
    room_code = normalize_room_code(request.args.get('room_code'))
    role = request.args.get('role', 'student')
    nickname = (request.args.get('nickname') or '').strip()
    if not room_code or role not in {'student', 'professor'}:
        return False
    if role == 'student' and (not nickname or len(nickname) > 30):
        return False

    join_room(room_code)
    connected_clients[request.sid] = {
        'room_code': room_code,
        'role': role,
        'nickname': nickname,
    }

    room_scoreboard = classroom_scoreboard.setdefault(room_code, {})
    if role == 'student':
        if nickname in room_scoreboard and any(
            client['room_code'] == room_code
            and client['role'] == 'student'
            and client['nickname'] == nickname
            for sid, client in connected_clients.items()
            if sid != request.sid
        ):
            connected_clients.pop(request.sid, None)
            return False
        room_scoreboard.setdefault(nickname, {'name': nickname, 'score': 0})

    state = get_room_state(room_code)
    if state['quiz_has_started'] and state['timer_is_active'] and state['active_question']:
        emit('incoming_quiz_question', {
            'question': state['active_question']['question'],
            'options': state['active_question']['options'],
            'seconds': state['timer_seconds'],
        })
        emit('timer_countdown_tick', {'seconds_left': state['timer_seconds']})

    emit('scoreboard_update', room_scoreboard, to=room_code)


@socketio.on('disconnect')
def handle_user_disconnect():
    connected_clients.pop(request.sid, None)


@socketio.on('submit_user_answer')
def process_user_submission(submission_package):
    user_id = request.sid
    client = connected_clients.get(user_id)
    if not client or client['role'] != 'student':
        return
    room_code = client['room_code']
    nickname = client['nickname']
    state = get_room_state(room_code)
    room_answers = quiz_answers.get(room_code, {})

    if (
        not state['timer_is_active']
        or state['timer_seconds'] <= 0
        or nickname not in classroom_scoreboard.get(room_code, {})
        or nickname in room_answers
    ):
        return

    chosen_index = submission_package.get('chosen_index')
    if not isinstance(chosen_index, int) or isinstance(chosen_index, bool):
        return
    if not state['active_question'] or not 0 <= chosen_index < len(state['active_question']['options']):
        return

    room_answers[nickname] = chosen_index
    if chosen_index == state['active_question']['correct_index']:
        classroom_scoreboard[room_code][nickname]['score'] += 10

    emit('scoreboard_update', classroom_scoreboard[room_code], to=room_code)

@socketio.on('drawing_data_from_browser')
def handle_incoming_drawing(coordinates_packages):
    client = connected_clients.get(request.sid)
    if client:
        emit(
            'drawing_data_from_server',
            coordinates_packages,
            to=client['room_code'],
            include_self=False,
        )

@socketio.on('clear_whiteboard_request')
def handle_board_wipe():
    client = connected_clients.get(request.sid)
    if client:
        emit('clear_whiteboard_command', to=client['room_code'])

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)