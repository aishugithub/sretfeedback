@echo off
REM ==========================================================================
REM  start-with-email.bat  —  one-click launcher that turns REAL email ON
REM --------------------------------------------------------------------------
REM  WHY THIS EXISTS
REM  The Flask app reads its SMTP settings from environment variables at
REM  startup (see emailer.py). If those variables aren't set in the SAME
REM  window that runs the server, the app quietly falls back to DEV mode and
REM  writes .eml files to app\outbox instead of sending. This script sets all
REM  six variables and launches the server in ONE window, so they can never
REM  get out of sync.
REM
REM  HOW TO USE
REM   1. Put your Gmail APP PASSWORD on the FEEDBACK_SMTP_PASS line below
REM      (16 characters, NO spaces). App password != your normal password,
REM      and it requires 2-Step Verification to be ON for the account.
REM   2. Save this file.
REM   3. Double-click it (or run it from a terminal). A window opens, prints
REM      the host it will use, and starts the server.
REM   4. Browse to http://localhost:5000/ and do a TEST-cycle send; the mail
REM      should arrive in the FEEDBACK_TEST_EMAIL inbox below.
REM
REM  SECURITY: this file will contain your app password once you fill it in.
REM  Keep it on your own machine; do NOT commit it to git or share it.
REM ==========================================================================

REM -- make sure we run from the app\ folder (where run.py lives) -------------
cd /d "%~dp0"

REM -- email configuration ---------------------------------------------------
set FEEDBACK_SMTP_HOST=smtp.gmail.com
set FEEDBACK_SMTP_PORT=587
set FEEDBACK_SMTP_USER=feedbacksret@gmail.com
set FEEDBACK_SMTP_PASS=uepreqxgfzcnxfoh
set FEEDBACK_SMTP_FROM=feedbacksret@gmail.com
set FEEDBACK_TEST_EMAIL=aishwarya.bramma@gmail.com

REM -- (optional) a strong cookie-signing key for production sessions --------
set FEEDBACK_SECRET_KEY=change-me-to-a-long-random-string

REM -- confirm what the app will see, then start it --------------------------
echo.
echo   SMTP host the app will use : %FEEDBACK_SMTP_HOST%
echo   Sending as                 : %FEEDBACK_SMTP_USER%
echo   Test-cycle mail goes to    : %FEEDBACK_TEST_EMAIL%
echo.
echo   If the host line above is blank, the file was not saved correctly.
echo   Starting server on http://localhost:5000/  (Ctrl-C to stop)
echo.

python run.py
pause
