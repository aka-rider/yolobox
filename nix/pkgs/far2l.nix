{ far2l, aws-sdk-cpp }:
(far2l.override { withGUI = false; withTTYX = false; }).overrideAttrs (old: {
  buildInputs = old.buildInputs ++ [ (aws-sdk-cpp.override { apis = [ "s3" ]; }) ];
  postInstall = old.postInstall + ''
    brokers=$out/lib/far2l/Plugins/NetRocks/plug
    for proto in FILE SHELL FTP SFTP SMB NFS WebDAV AWS; do
      test -x $brokers/NetRocks-$proto.broker \
        || { echo "far2l: NetRocks-$proto missing" >&2; exit 1; }
    done
    $READELF -d $brokers/NetRocks-FTP.broker | grep -q libssl \
      || { echo "far2l: NetRocks-FTP built without OpenSSL (no FTPS)" >&2; exit 1; }
  '';
})
