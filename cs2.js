const SteamUser = require('steam-user');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const client = new SteamUser();

const SECRETS_DIR = path.join(__dirname, 'secrets');
const CREDENTIALS_FILE = path.join(SECRETS_DIR, 'steam_presence.json');
const REFRESH_TOKEN_FILE = path.join(SECRETS_DIR, 'refreshToken.txt');
const CREDENTIALS_TEMPLATE = {
    accountName: 'tu_usuario_steam',
    password: 'tu_password_steam'
};

let logOnOptions;
let pendingSteamGuardCallback = null;
let steamGuardSatisfied = false;

const stdinInterface = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false
});

stdinInterface.on('line', (line) => {
    if (!pendingSteamGuardCallback) {
        return;
    }

    const code = line.trim();
    if (!code) {
        terminateProcess(2, 'No se recibio codigo de Steam Guard.');
        return;
    }

    const callback = pendingSteamGuardCallback;
    pendingSteamGuardCallback = null;
    steamGuardSatisfied = true;
    callback(code);
});

if (fs.existsSync(REFRESH_TOKEN_FILE)) {
    logOnOptions = {
        refreshToken: fs.readFileSync(REFRESH_TOKEN_FILE, 'utf8').trim()
    };
} else if (fs.existsSync(CREDENTIALS_FILE)) {
    const credentials = JSON.parse(fs.readFileSync(CREDENTIALS_FILE, 'utf8'));

    if (!credentials.accountName || !credentials.password) {
        throw new Error(
            `El archivo ${CREDENTIALS_FILE} debe contener accountName y password.`
        );
    }

    logOnOptions = {
        accountName: credentials.accountName,
        password: credentials.password
    };
} else {
    fs.mkdirSync(SECRETS_DIR, { recursive: true });
    fs.writeFileSync(
        CREDENTIALS_FILE,
        JSON.stringify(CREDENTIALS_TEMPLATE, null, 2) + '\n'
    );
    throw new Error(
        `No existe ${REFRESH_TOKEN_FILE}. Se ha generado ${CREDENTIALS_FILE}; rellena accountName y password y vuelve a ejecutar.`
    );
}

function terminateProcess(exitCode, message) {
    if (message) {
        console.error(message);
    }

    pendingSteamGuardCallback = null;
    stdinInterface.close();
    process.exit(exitCode);
}

client.logOn(logOnOptions);

client.on('steamGuard', (domain, callback) => {
    if (steamGuardSatisfied) {
        terminateProcess(2, 'Steam Guard solicitado de nuevo tras enviar un codigo.');
        return;
    }

    const guardType = domain ? `email (${domain})` : 'Steam Guard App';
    pendingSteamGuardCallback = callback;
    console.log(`STEAM_GUARD_CODE_REQUIRED:${guardType}`);
});

client.on('refreshToken', (token) => {
    fs.mkdirSync(SECRETS_DIR, { recursive: true });
    fs.writeFileSync(REFRESH_TOKEN_FILE, token);
    console.log('Refresh token guardado');
});

client.on('loggedOn', () => {
    console.log('Conectado');
    client.gamesPlayed([730]);
});

client.on('error', (error) => {
    if (error && error.message === 'RateLimitExceeded') {
        terminateProcess(
            3,
            'RateLimitExceeded: Steam ha limitado temporalmente los intentos de login. Espera unos minutos antes de reintentar.'
        );
        return;
    }

    console.error(error);
    terminateProcess(1);
});
