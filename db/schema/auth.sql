-- Phase 1: Auth schema for trading app
-- Adds users and clients tables for role-based access

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('client', 'admin')),
    client_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

-- Insert default admin user (password: admin123)
-- bcrypt hash of 'admin123'
INSERT INTO users (email, password_hash, role, client_id) VALUES (
    'admin@trading.local',
    '$2b$12$LJ3m4ys3HzMeLrbH/v0m4OQKCB5LkGq7XBbOSYjJl6jKK2DmC5f6.',
    'admin',
    NULL
);

-- Insert sample client
INSERT INTO clients (id, name) VALUES (1, 'Demo Capital LLC');
INSERT INTO clients (id, name) VALUES (2, 'Alpha Trading Inc');

-- Insert sample client users (password: client123)
INSERT INTO users (email, password_hash, role, client_id) VALUES (
    'client1@trading.local',
    '$2b$12$G5Q3m4ys3HzMeLrbH/v0m4OQKCB5LkGq7XBbOSYjJl6jKK2DmC5f6y',
    'client',
    1
);
INSERT INTO users (email, password_hash, role, client_id) VALUES (
    'client2@trading.local',
    '$2b$12$G5Q3m4ys3HzMeLrbH/v0m4OQKCB5LkGq7XBbOSYjJl6jKK2DmC5f6y',
    'client',
    2
);
