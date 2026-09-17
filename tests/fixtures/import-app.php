<?php
// Synthetic PHP 7.0-compatible import fixture. No production data or integrations.
$config = require __DIR__.'/sites/default/settings.php';
$engine = getenv('DATABASE_ENGINE');
$dsn = ($engine === 'postgres' ? 'pgsql' : 'mysql').':host='.getenv('DATABASE_HOST').';port='.getenv('DATABASE_PORT').';dbname='.getenv('DATABASE_NAME');
$db = new PDO($dsn, getenv('DATABASE_USER'), getenv('DATABASE_PASSWORD'), array(PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION));
if (PHP_SAPI !== 'cli' && $_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!isset($_FILES['upload']) || !move_uploaded_file($_FILES['upload']['tmp_name'], __DIR__.'/sites/default/files/web-upload.txt')) {
        http_response_code(400); exit('Upload failed');
    }
}
$rows = $db->query('SELECT id,payload FROM acceptance_records ORDER BY id')->fetchAll(PDO::FETCH_NUM);
foreach ($rows as &$row) { $row[0] = (int)$row[0]; } unset($row);
$files = array();
foreach (array('restored.txt', 'panel-upload.txt', 'web-upload.txt', 'cli-upload.txt') as $name) {
    $path = __DIR__.'/sites/default/files/'.$name;
    if (is_file($path)) { $files[$name] = array('uid' => fileowner($path), 'sha256' => hash_file('sha256', $path)); }
}
header('Content-Type: application/json');
echo json_encode(array('label' => $config['label'], 'php' => PHP_VERSION, 'engine' => $engine,
    'database_version' => $db->query('SELECT VERSION()')->fetchColumn(), 'uid' => posix_geteuid(),
    'rows' => $rows, 'export_sha256' => hash('sha256', json_encode($rows)), 'files' => $files,
    'https' => isset($_SERVER['HTTPS']) ? $_SERVER['HTTPS'] : null,
    'query' => $_GET), JSON_UNESCAPED_SLASHES);
