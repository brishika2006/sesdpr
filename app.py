
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, Response
)
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from flask_bcrypt import Bcrypt
from database import get_connection, create_tables
from datetime import date, datetime
import csv
import io

app = Flask(__name__)
app.config["SECRET_KEY"] = "expense-tracker-secret-key-change-before-deployment"

bcrypt = Bcrypt(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

create_tables()


# ---------------- USER CLASS ----------------

class User(UserMixin):

    def __init__(self, user_id, name, email):
        self.id = user_id
        self.name = name
        self.email = email


@login_manager.user_loader
def load_user(user_id):

    connection = get_connection()

    user = connection.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    connection.close()

    if user:
        return User(user["user_id"], user["name"], user["email"])

    return None


# ---------------- HOME ----------------

@app.route("/")
def home():

    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# ---------------- REGISTER ----------------

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Please fill in all fields.")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return redirect(url_for("register"))

        hashed_password = bcrypt.generate_password_hash(
            password
        ).decode("utf-8")

        connection = get_connection()

        try:

            cursor = connection.execute(
                """
                INSERT INTO users (name, email, password)
                VALUES (?, ?, ?)
                """,
                (name, email, hashed_password)
            )

            connection.commit()

            user = User(cursor.lastrowid, name, email)
            login_user(user)

            flash("Registration successful!")
            return redirect(url_for("dashboard"))

        except Exception:

            connection.rollback()
            flash("This email may already be registered.")

        finally:
            connection.close()

    return render_template("register.html")


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        connection = get_connection()

        user = connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        connection.close()

        if user and bcrypt.check_password_hash(
            user["password"], password
        ):

            login_user(
                User(user["user_id"], user["name"], user["email"])
            )

            return redirect(url_for("dashboard"))

        flash("Invalid email or password.")

    return render_template("login.html")


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():

    connection = get_connection()
    today = date.today()

    # All transactions belonging to the logged-in user
    transactions = connection.execute(
        """
        SELECT
            t.transaction_id,
            t.amount,
            t.transaction_type,
            t.description,
            t.transaction_date,
            c.category_name
        FROM transactions t
        LEFT JOIN categories c
            ON t.category_id = c.category_id
        WHERE t.user_id = ?
        ORDER BY t.transaction_date DESC, t.transaction_id DESC
        """,
        (current_user.id,)
    ).fetchall()

    # Total income
    income = connection.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE user_id = ?
          AND transaction_type = 'Income'
        """,
        (current_user.id,)
    ).fetchone()["total"]

    # Total expenses
    expenses = connection.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE user_id = ?
          AND transaction_type = 'Expense'
        """,
        (current_user.id,)
    ).fetchone()["total"]

    balance = income - expenses

    # ---------------- BAR CHART DATA ----------------

    monthly_rows = connection.execute(
        """
        SELECT
            strftime('%Y-%m', transaction_date) AS month,
            SUM(
                CASE WHEN transaction_type = 'Income'
                THEN amount ELSE 0 END
            ) AS income,
            SUM(
                CASE WHEN transaction_type = 'Expense'
                THEN amount ELSE 0 END
            ) AS expenses
        FROM transactions
        WHERE user_id = ?
          AND date(transaction_date) >=
              date('now', 'start of month', '-5 months')
        GROUP BY strftime('%Y-%m', transaction_date)
        ORDER BY month
        """,
        (current_user.id,)
    ).fetchall()

    monthly_summary = [dict(row) for row in monthly_rows]

    # ---------------- PIE CHART DATA ----------------

    category_rows = connection.execute(
        """
        SELECT
            c.category_name,
            SUM(t.amount) AS total
        FROM transactions t
        JOIN categories c
            ON t.category_id = c.category_id
        WHERE t.user_id = ?
          AND t.transaction_type = 'Expense'
        GROUP BY c.category_name
        ORDER BY total DESC
        """,
        (current_user.id,)
    ).fetchall()

    category_summary = [dict(row) for row in category_rows]

    # ---------------- MONTHLY BUDGETS ----------------

    budget_rows = connection.execute(
        """
        SELECT
            b.monthly_limit,
            c.category_name,
            COALESCE(SUM(
                CASE
                    WHEN t.transaction_type = 'Expense'
                    THEN t.amount
                    ELSE 0
                END
            ), 0) AS spent
        FROM budgets b
        JOIN categories c
            ON b.category_id = c.category_id
        LEFT JOIN transactions t
            ON t.user_id = b.user_id
            AND t.category_id = b.category_id
            AND strftime('%m', t.transaction_date) =
                printf('%02d', b.month)
            AND strftime('%Y', t.transaction_date) =
                CAST(b.year AS TEXT)
        WHERE b.user_id = ?
          AND b.month = ?
          AND b.year = ?
        GROUP BY b.budget_id, c.category_name
        """,
        (current_user.id, today.month, today.year)
    ).fetchall()

    # Calculate budget progress and warning status
    budgets = []

    for row in budget_rows:

        budget = dict(row)
        limit_amount = float(budget["monthly_limit"])
        spent_amount = float(budget["spent"])

        percentage = (spent_amount / limit_amount) * 100

        budget["percentage"] = round(percentage, 1)
        budget["progress_width"] = min(percentage, 100)

        if percentage >= 100:
            budget["status"] = "exceeded"
            budget["warning"] = "Budget exceeded!"
        elif percentage >= 80:
            budget["status"] = "warning"
            budget["warning"] = "You have used 80% or more of your budget."
        else:
            budget["status"] = "normal"
            budget["warning"] = ""

        budget["remaining"] = limit_amount - spent_amount

        budgets.append(budget)

    # ---------------- EXPENSE PREDICTION ----------------

    # Average expenses from the previous 3 complete months.
    prediction_row = connection.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE user_id = ?
          AND transaction_type = 'Expense'
          AND date(transaction_date) >=
              date('now', 'start of month', '-3 months')
          AND date(transaction_date) <
              date('now', 'start of month')
        """,
        (current_user.id,)
    ).fetchone()

    previous_three_month_total = float(prediction_row["total"])
    predicted_expenses = previous_three_month_total / 3

    connection.close()

    return render_template(
        "dashboard.html",
        name=current_user.name,
        transactions=transactions,
        total_income=income,
        total_expenses=expenses,
        balance=balance,
        budgets=budgets,
        current_month=today.strftime("%B %Y"),
        monthly_summary=monthly_summary,
        category_summary=category_summary,
        predicted_expenses=predicted_expenses,
        report_month=today.strftime("%Y-%m")
    )


# ---------------- ADD TRANSACTION ----------------

@app.route("/add_transaction", methods=["POST"])
@login_required
def add_transaction():

    amount = request.form.get("amount", "")
    transaction_type = request.form.get("transaction_type", "")
    category_name = request.form.get("category", "").strip().title()
    description = request.form.get("description", "").strip()
    transaction_date = request.form.get("transaction_date", "")

    try:

        amount = float(amount)

        if amount <= 0:
            flash("Amount must be greater than zero.")
            return redirect(url_for("dashboard"))

        if transaction_type not in ["Income", "Expense"]:
            flash("Invalid transaction type.")
            return redirect(url_for("dashboard"))

        if not category_name:
            flash("Please enter a category.")
            return redirect(url_for("dashboard"))

        datetime.strptime(transaction_date, "%Y-%m-%d")

        connection = get_connection()

        category = connection.execute(
            """
            SELECT category_id FROM categories
            WHERE category_name = ?
            """,
            (category_name,)
        ).fetchone()

        if category:
            category_id = category["category_id"]

        else:

            cursor = connection.execute(
                """
                INSERT INTO categories (category_name)
                VALUES (?)
                """,
                (category_name,)
            )

            category_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO transactions
            (
                user_id,
                category_id,
                amount,
                transaction_type,
                description,
                transaction_date
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                current_user.id,
                category_id,
                amount,
                transaction_type,
                description,
                transaction_date
            )
        )

        connection.commit()
        connection.close()

        flash("Transaction added successfully!")

    except ValueError:
        flash("Please enter a valid amount and date.")

    return redirect(url_for("dashboard"))


# ---------------- SET MONTHLY BUDGET ----------------

@app.route("/set_budget", methods=["POST"])
@login_required
def set_budget():

    category_name = request.form.get("category", "").strip().title()
    monthly_limit = request.form.get("monthly_limit", "")

    try:

        monthly_limit = float(monthly_limit)

        if not category_name or monthly_limit <= 0:
            flash("Enter a valid category and budget.")
            return redirect(url_for("dashboard"))

        today = date.today()
        connection = get_connection()

        category = connection.execute(
            """
            SELECT category_id FROM categories
            WHERE category_name = ?
            """,
            (category_name,)
        ).fetchone()

        if category:
            category_id = category["category_id"]

        else:

            cursor = connection.execute(
                """
                INSERT INTO categories (category_name)
                VALUES (?)
                """,
                (category_name,)
            )

            category_id = cursor.lastrowid

        existing = connection.execute(
            """
            SELECT budget_id FROM budgets
            WHERE user_id = ?
              AND category_id = ?
              AND month = ?
              AND year = ?
            """,
            (current_user.id, category_id, today.month, today.year)
        ).fetchone()

        if existing:

            connection.execute(
                """
                UPDATE budgets
                SET monthly_limit = ?
                WHERE budget_id = ?
                """,
                (monthly_limit, existing["budget_id"])
            )

        else:

            connection.execute(
                """
                INSERT INTO budgets
                (user_id, category_id, monthly_limit, month, year)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    current_user.id,
                    category_id,
                    monthly_limit,
                    today.month,
                    today.year
                )
            )

        connection.commit()
        connection.close()

        flash("Monthly budget saved successfully!")

    except ValueError:
        flash("Please enter a valid budget amount.")

    return redirect(url_for("dashboard"))


# ---------------- DOWNLOAD MONTHLY CSV REPORT ----------------

@app.route("/download_report", methods=["GET"])
@login_required
def download_report():

    selected_month = request.args.get("month", "")

    try:
        month_date = datetime.strptime(selected_month, "%Y-%m")
        selected_month = month_date.strftime("%Y-%m")

    except ValueError:
        flash("Please select a valid month.")
        return redirect(url_for("dashboard"))

    connection = get_connection()

    report_transactions = connection.execute(
        """
        SELECT
            t.transaction_date,
            t.transaction_type,
            COALESCE(c.category_name, 'Uncategorized') AS category_name,
            t.description,
            t.amount
        FROM transactions t
        LEFT JOIN categories c
            ON t.category_id = c.category_id
        WHERE t.user_id = ?
          AND strftime('%Y-%m', t.transaction_date) = ?
        ORDER BY t.transaction_date, t.transaction_id
        """,
        (current_user.id, selected_month)
    ).fetchall()

    totals = connection.execute(
        """
        SELECT
            COALESCE(SUM(
                CASE WHEN transaction_type = 'Income'
                THEN amount ELSE 0 END
            ), 0) AS income,
            COALESCE(SUM(
                CASE WHEN transaction_type = 'Expense'
                THEN amount ELSE 0 END
            ), 0) AS expenses
        FROM transactions
        WHERE user_id = ?
          AND strftime('%Y-%m', transaction_date) = ?
        """,
        (current_user.id, selected_month)
    ).fetchone()

    connection.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["Monthly Expense Tracker Report"])
    writer.writerow(["Month", selected_month])
    writer.writerow(["Total Income", round(float(totals["income"]), 2)])
    writer.writerow(["Total Expenses", round(float(totals["expenses"]), 2)])
    writer.writerow([
        "Balance",
        round(float(totals["income"]) - float(totals["expenses"]), 2)
    ])
    writer.writerow([])
    writer.writerow([
        "Date", "Type", "Category", "Description", "Amount"
    ])

    for transaction in report_transactions:
        writer.writerow([
            transaction["transaction_date"],
            transaction["transaction_type"],
            transaction["category_name"],
            transaction["description"],
            transaction["amount"]
        ])

    filename = f"expense_report_{selected_month}.csv"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )


# ---------------- LOGOUT ----------------

@app.route("/logout")
@login_required
def logout():

    logout_user()

    return redirect(url_for("login"))


# ---------------- RUN APPLICATION ----------------

if __name__ == "__main__":
    app.run(debug=True)
