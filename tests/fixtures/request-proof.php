<?php
session_start(); session_write_close();
$keys=array('REMOTE_ADDR','HTTP_HOST','HTTPS','SERVER_PORT','REQUEST_SCHEME','HTTP_X_FORWARDED_FOR','HTTP_X_FORWARDED_PROTO','HTTP_X_FORWARDED_HOST','HTTP_X_FORWARDED_PORT','HTTP_FORWARDED','HTTP_X_REAL_IP','REQUEST_URI');
$out=array('uid'=>posix_geteuid(),'php'=>PHP_VERSION,'query'=>$_GET);
foreach ($keys as $key) $out[$key]=isset($_SERVER[$key])?$_SERVER[$key]:null;
if (isset($_GET['loopback'])) {
    $c=curl_init('https://'.$_SERVER['HTTP_HOST'].'/request-proof.php?inner=1');
    curl_setopt_array($c,array(CURLOPT_RETURNTRANSFER=>true,CURLOPT_TIMEOUT=>10));
    $body=curl_exec($c); $out['loopback']=array('status'=>curl_getinfo($c,CURLINFO_HTTP_CODE),'verify'=>curl_getinfo($c,CURLINFO_SSL_VERIFYRESULT),'error'=>curl_error($c),'body'=>json_decode($body,true));
    curl_close($c);
}
header('Content-Type: application/json'); echo json_encode($out);
