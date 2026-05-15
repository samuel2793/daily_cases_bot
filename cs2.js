const SteamUser = require('steam-user');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const client = new SteamUser({
    renewRefreshTokens: true
});

const SECRETS_DIR = path.join(__dirname, 'secrets');
const CREDENTIALS_FILE = path.join(SECRETS_DIR, 'steam_presence.json');
const REFRESH_TOKEN_FILE = path.join(SECRETS_DIR, 'refreshToken.txt');
const CREDENTIALS_TEMPLATE = {
    accountName: 'tu_usuario_steam',
    password: 'tu_password_steam'
};
const PLACEHOLDER_VALUES = new Set([
    CREDENTIALS_TEMPLATE.accountName,
    CREDENTIALS_TEMPLATE.password
]);

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

function terminateProcess(exitCode, message) {
    if (message) {
        console.error(message);
    }

    pendingSteamGuardCallback = null;
    stdinInterface.close();
    process.exit(exitCode);
}

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

    const startTime = Date.now();

    // Función para formatear el tiempo transcurrido en HH:MM:SS
    function getTiempoTranscurrido() {
        const tiempoSegundos = Math.floor((Date.now() - startTime) / 1000);
        const horas = Math.floor(tiempoSegundos / 3600);
        const minutos = Math.floor((tiempoSegundos % 3600) / 60);
        const segundos = tiempoSegundos % 60;
        return `${horas.toString().padStart(2, '0')}:${minutos
            .toString()
            .padStart(2, '0')}:${segundos.toString().padStart(2, '0')}`;
    }

    // Intervalo que actualiza el estado cada segundo
    const interval = setInterval(() => {
        process.stdout.write(`\rConectado | ${getTiempoTranscurrido()}`);
    }, 1000);

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

function ensureSecretsDir() {
    fs.mkdirSync(SECRETS_DIR, { recursive: true });
}

function hasUsableCredentials(credentials) {
    return Boolean(
        credentials &&
        credentials.accountName &&
        credentials.password &&
        !PLACEHOLDER_VALUES.has(credentials.accountName) &&
        !PLACEHOLDER_VALUES.has(credentials.password)
    );
}

function loadCredentialsIfPresent() {
    if (!fs.existsSync(CREDENTIALS_FILE)) {
        return null;
    }

    const credentials = JSON.parse(fs.readFileSync(CREDENTIALS_FILE, 'utf8'));
    if (!hasUsableCredentials(credentials)) {
        return null;
    }

    return credentials;
}

function writeCredentialsTemplateIfMissing() {
    ensureSecretsDir();
    if (fs.existsSync(CREDENTIALS_FILE)) {
        return;
    }

    fs.writeFileSync(
        CREDENTIALS_FILE,
        JSON.stringify(CREDENTIALS_TEMPLATE, null, 2) + '\n'
    );
}

async function bootstrapRefreshTokenWithQR() {
    ensureSecretsDir();
    writeCredentialsTemplateIfMissing();

    let qrcodeTerminal;
    let LoginSession;
    let EAuthTokenPlatformType;

    try {
        qrcodeTerminal = require('qrcode-terminal');
        ({
            LoginSession,
            EAuthTokenPlatformType
        } = require('steam-session'));
    } catch (error) {
        throw new Error(
            "Faltan dependencias para el login por QR de Steam Presence. Ejecuta 'npm install' en este directorio."
        );
    }

    const loginSession = new LoginSession(EAuthTokenPlatformType.SteamClient);
    loginSession.loginTimeout = 180000;

    return await new Promise(async (resolve, reject) => {
        let settled = false;

        const finishResolve = (value) => {
            if (settled) {
                return;
            }
            settled = true;
            resolve(value);
        };

        const finishReject = (error) => {
            if (settled) {
                return;
            }
            settled = true;
            reject(error);
        };

        loginSession.on('authenticated', () => {
            if (!loginSession.refreshToken) {
                finishReject(new Error('Steam QR autenticado pero sin refresh token.'));
                return;
            }

            fs.writeFileSync(REFRESH_TOKEN_FILE, loginSession.refreshToken);
            console.log('Refresh token guardado');
            finishResolve({
                refreshToken: loginSession.refreshToken
            });
        });

        loginSession.on('timeout', () => {
            finishReject(new Error('QR de Steam expirado antes de ser confirmado.'));
        });

        loginSession.on('remoteInteraction', () => {
            console.log('QR de Steam escaneado. Confirma el acceso en Steam Guard.');
        });

        loginSession.on('error', (error) => {
            finishReject(error);
        });

        try {
            const startResult = await loginSession.startWithQR();
            console.log('Escanea este QR con Steam Guard para autorizar Steam Presence:');
            qrcodeTerminal.generate(startResult.qrChallengeUrl, {small: true});
            console.log(startResult.qrChallengeUrl);
        } catch (error) {
            finishReject(error);
        }
    });
}

async function resolveLogOnOptions() {
    if (fs.existsSync(REFRESH_TOKEN_FILE)) {
        return {
            refreshToken: fs.readFileSync(REFRESH_TOKEN_FILE, 'utf8').trim()
        };
    }

    const credentials = loadCredentialsIfPresent();
    if (credentials) {
        return {
            accountName: credentials.accountName,
            password: credentials.password
        };
    }

    return await bootstrapRefreshTokenWithQR();
}

(async () => {
    try {
        logOnOptions = await resolveLogOnOptions();
        client.logOn(logOnOptions);
    } catch (error) {
        terminateProcess(
            1,
            error && error.message
                ? error.message
                : 'No se pudo inicializar Steam Presence.'
        );
    }
})();
