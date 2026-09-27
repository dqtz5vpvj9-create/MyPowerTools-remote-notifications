using MyPowerTools.Platform.Abstractions;
using RemoteNotifications.Surface.Services;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;
using Org.BouncyCastle.Crypto.Utilities;
using Org.BouncyCastle.Utilities.IO.Pem;

namespace RemoteNotifications.Android.Tests;

/// <summary>
/// The Android signing identity must produce the same deterministic signature as the desktop signer,
/// must live only in the platform credential store, and must never leak key material through files,
/// logs or command output.
/// </summary>
public sealed class SigningKeyTests
{
    private static readonly byte[] Handshake = System.Text.Encoding.ASCII.GetBytes("hello");

    [Fact]
    public async Task Signature_matches_the_ed25519_hello_contract()
    {
        using var key = TestSigningKey.Create();
        var signing = new RemoteNotificationSigningKey(
            new InMemorySecretStore(),
            RemoteNotificationsAndroidOptions.ModuleId);
        await signing.SaveAsync(key.Pem, CancellationToken.None);

        var signature = await signing.GetHandshakeSignatureAsync(CancellationToken.None);

        var signer = new Ed25519Signer();
        signer.Init(forSigning: true, key.PrivateKey);
        signer.BlockUpdate(Handshake, 0, Handshake.Length);
        var expected = Convert.ToBase64String(signer.GenerateSignature()).Replace('+', '-').Replace('/', '_');
        Assert.Equal(expected, signature);

        var verifier = new Ed25519Signer();
        verifier.Init(forSigning: false, key.PrivateKey.GeneratePublicKey());
        verifier.BlockUpdate(Handshake, 0, Handshake.Length);
        var raw = Convert.FromBase64String(signature.Replace('-', '+').Replace('_', '/'));
        Assert.True(verifier.VerifySignature(raw));
    }

    [Fact]
    public async Task Imported_key_is_stored_only_in_the_platform_credential_store()
    {
        using var key = TestSigningKey.Create();
        await using var harness = new ModuleHarness();
        await harness.InitializeAsync();

        var import = await harness.ExecuteAsync(
            RemoteNotificationsAndroidOptions.CommandSigningKeyImport,
            new JsonObject { [RemoteNotificationsAndroidOptions.SigningKeyArgument] = key.Pem });
        Assert.True(import.Success, import.Error?.Message);
        Assert.DoesNotContain("OPENSSH PRIVATE KEY", import.Output, StringComparison.Ordinal);
        Assert.DoesNotContain(key.Pem[..40], import.Output, StringComparison.Ordinal);

        var stored = await harness.Secrets.ReadAsync(
            SecretReference.Create(RemoteNotificationsAndroidOptions.ModuleId, RemoteNotificationsAndroidOptions.SigningKeySecretName),
            CancellationToken.None);
        Assert.Equal(key.Pem, stored);

        // No plaintext key anywhere in the module's private data or the tool's private data.
        foreach (var directory in new[] { harness.DataDirectory, harness.ToolDataDirectory })
        {
            if (!Directory.Exists(directory))
            {
                continue;
            }

            foreach (var file in Directory.EnumerateFiles(directory, "*", SearchOption.AllDirectories))
            {
                var content = await File.ReadAllTextAsync(file);
                Assert.DoesNotContain("OPENSSH PRIVATE KEY", content, StringComparison.Ordinal);
            }
        }
    }

    [Fact]
    public async Task Cached_signature_is_invalidated_when_the_key_changes_or_is_cleared()
    {
        using var first = TestSigningKey.Create();
        using var second = TestSigningKey.Create(seedOffset: 7);
        var signing = new RemoteNotificationSigningKey(
            new InMemorySecretStore(),
            RemoteNotificationsAndroidOptions.ModuleId);

        await signing.SaveAsync(first.Pem, CancellationToken.None);
        var firstSignature = await signing.GetHandshakeSignatureAsync(CancellationToken.None);
        Assert.Equal(firstSignature, await signing.GetHandshakeSignatureAsync(CancellationToken.None));

        await signing.SaveAsync(second.Pem, CancellationToken.None);
        var secondSignature = await signing.GetHandshakeSignatureAsync(CancellationToken.None);
        Assert.NotEqual(firstSignature, secondSignature);
        Assert.Equal(RemoteNotificationSigningKey.SignHandshakePem(second.Pem), secondSignature);

        await signing.ClearAsync(CancellationToken.None);
        Assert.False(await signing.IsConfiguredAsync(CancellationToken.None));
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            signing.GetHandshakeSignatureAsync(CancellationToken.None));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not a pem document")]
    [InlineData("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")]
    public async Task Invalid_key_material_is_rejected_without_echoing_it(string material)
    {
        var signing = new RemoteNotificationSigningKey(
            new InMemorySecretStore(),
            RemoteNotificationsAndroidOptions.ModuleId);

        var exception = await Assert.ThrowsAsync<InvalidDataException>(() =>
            signing.SaveAsync(material, CancellationToken.None));
        Assert.DoesNotContain("AAAA", exception.Message, StringComparison.Ordinal);
        Assert.DoesNotContain("not a pem document", exception.Message, StringComparison.Ordinal);
        Assert.False(await signing.IsConfiguredAsync(CancellationToken.None));
    }

    [Fact]
    public async Task Private_key_in_pkcs8_form_is_rejected_because_the_protocol_uses_openssh_keys()
    {
        using var key = TestSigningKey.Create();
        var pkcs8 = Convert.ToBase64String(key.PrivateKey.GetEncoded());
        var pem = $"-----BEGIN PRIVATE KEY-----\n{pkcs8}\n-----END PRIVATE KEY-----\n";
        var signing = new RemoteNotificationSigningKey(
            new InMemorySecretStore(),
            RemoteNotificationsAndroidOptions.ModuleId);

        var exception = await Assert.ThrowsAsync<InvalidDataException>(() =>
            signing.SaveAsync(pem, CancellationToken.None));
        Assert.Contains("OpenSSH", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Sign_handshake_is_deterministic_across_calls()
    {
        using var key = TestSigningKey.Create();
        Assert.Equal(
            RemoteNotificationSigningKey.SignHandshakePem(key.Pem),
            RemoteNotificationSigningKey.SignHandshakePem(key.Pem));
    }

    [Fact]
    public async Task Key_clear_command_stops_background_polling_and_reports_no_key()
    {
        using var key = TestSigningKey.Create();
        await using var harness = new ModuleHarness();
        // The background loop must never open a socket from a test, so the pull client is scripted.
        await harness.UseScriptedPollerAsync(_ => new RemoteNotificationPullResult("idle", [], ""));
        await harness.InitializeAsync();
        await harness.ExecuteAsync(
            RemoteNotificationsAndroidOptions.CommandSigningKeyImport,
            new JsonObject { [RemoteNotificationsAndroidOptions.SigningKeyArgument] = key.Pem });

        var started = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandPollingStart);
        Assert.True(started.Success, started.Error?.Message);
        var cleared = await harness.ExecuteAsync(RemoteNotificationsAndroidOptions.CommandSigningKeyClear);

        Assert.True(cleared.Success, cleared.Error?.Message);
        Assert.Contains("\"keyConfigured\": false", cleared.Output, StringComparison.Ordinal);
        Assert.Equal(0, harness.Background.ActiveCount);
    }
}
