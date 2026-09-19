-- ===========================================================================
-- Freio de tentativas do login do painel
-- ===========================================================================
--
-- Mesmo desenho do `auth_throttle` do terminal, e pelo mesmo motivo: contador
-- em memória vira "reinicia o processo e ganha mais tentativas". Aqui o
-- processo é um contêiner que o Coolify pode reiniciar a qualquer momento — um
-- deploy, um OOM, um health check falhando —, então o freio precisa viver no
-- banco para significar alguma coisa.
--
-- `scope` guarda `email:<endereco>` e `ip:<origem>`. Os dois, somados:
--
-- * só por IP, um atacante atrás de várias saídas passa livre;
-- * só por e-mail, ele trava a conta do dono de propósito para tirá-lo do ar,
--   que é negação de serviço barata demais para deixar aberta.

CREATE TABLE IF NOT EXISTS panel_login_throttle (
    scope            TEXT PRIMARY KEY,
    failures         INTEGER NOT NULL DEFAULT 0,
    first_failure_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_failure_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_panel_throttle_locked
    ON panel_login_throttle (locked_until);
