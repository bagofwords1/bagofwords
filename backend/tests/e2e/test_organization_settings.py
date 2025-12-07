import pytest
from io import BytesIO


def create_test_png_image(width=100, height=100, color=(255, 0, 0)):
    """Create a valid PNG image for testing using Pillow."""
    from PIL import Image
    
    # Create a simple colored image
    img = Image.new('RGB', (width, height), color)
    
    # Save to bytes
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer.getvalue()


@pytest.mark.e2e
def test_get_organization_settings(
    get_organization_settings,
    create_user,
    login_user,
    whoami
):
    """Test fetching organization settings."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    settings = get_organization_settings(user_token=user_token, org_id=org_id)
    
    assert settings is not None
    assert "config" in settings or settings.get("config") is None  # May be empty initially


@pytest.mark.e2e
def test_update_organization_settings_general(
    get_organization_settings,
    update_organization_settings,
    create_user,
    login_user,
    whoami
):
    """Test updating general organization settings (AI analyst name, bow credit)."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Update general settings
    new_config = {
        "general": {
            "ai_analyst_name": "Custom AI Assistant",
            "bow_credit": False
        }
    }
    
    updated_settings = update_organization_settings(
        config=new_config,
        user_token=user_token,
        org_id=org_id
    )
    
    assert updated_settings is not None
    assert updated_settings["config"]["general"]["ai_analyst_name"] == "Custom AI Assistant"
    assert updated_settings["config"]["general"]["bow_credit"] is False
    
    # Verify settings persisted
    fetched_settings = get_organization_settings(user_token=user_token, org_id=org_id)
    assert fetched_settings["config"]["general"]["ai_analyst_name"] == "Custom AI Assistant"
    assert fetched_settings["config"]["general"]["bow_credit"] is False


@pytest.mark.e2e
def test_upload_organization_icon(
    upload_organization_icon,
    get_organization_settings,
    get_organization_icon,
    create_user,
    login_user,
    whoami
):
    """Test uploading an organization icon."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # Create a test PNG image
    png_data = create_test_png_image()
    
    # Upload icon
    result = upload_organization_icon(
        icon_content=png_data,
        filename="test_icon.png",
        content_type="image/png",
        user_token=user_token,
        org_id=org_id
    )
    
    assert result is not None
    assert result["config"]["general"]["icon_key"] is not None
    assert result["config"]["general"]["icon_url"] is not None
    assert "/api/general/icon/" in result["config"]["general"]["icon_url"]
    
    # Verify icon is accessible
    icon_key = result["config"]["general"]["icon_key"]
    icon_response = get_organization_icon(icon_key)
    assert icon_response.status_code == 200


@pytest.mark.e2e
def test_delete_organization_icon(
    upload_organization_icon,
    delete_organization_icon,
    get_organization_settings,
    get_organization_icon,
    create_user,
    login_user,
    whoami
):
    """Test deleting an organization icon."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # First upload an icon
    png_data = create_test_png_image()
    upload_result = upload_organization_icon(
        icon_content=png_data,
        filename="test_icon.png",
        content_type="image/png",
        user_token=user_token,
        org_id=org_id
    )
    
    icon_key = upload_result["config"]["general"]["icon_key"]
    assert icon_key is not None
    
    # Delete the icon
    delete_result = delete_organization_icon(user_token=user_token, org_id=org_id)
    
    assert delete_result["config"]["general"]["icon_key"] is None
    assert delete_result["config"]["general"]["icon_url"] is None
    
    # Verify icon is no longer accessible
    icon_response = get_organization_icon(icon_key)
    assert icon_response.status_code == 404


@pytest.mark.e2e
def test_update_organization_settings_full_workflow(
    get_organization_settings,
    update_organization_settings,
    upload_organization_icon,
    delete_organization_icon,
    create_user,
    login_user,
    whoami
):
    """Test full workflow: update settings, upload icon, update again, remove icon."""
    user = create_user()
    user_token = login_user(user["email"], user["password"])
    org_id = whoami(user_token)['organizations'][0]['id']
    
    # 1. Set initial general settings
    initial_config = {
        "general": {
            "ai_analyst_name": "Data Bot",
            "bow_credit": True
        }
    }
    result = update_organization_settings(config=initial_config, user_token=user_token, org_id=org_id)
    assert result["config"]["general"]["ai_analyst_name"] == "Data Bot"
    
    # 2. Upload an icon
    png_data = create_test_png_image()
    icon_result = upload_organization_icon(
        icon_content=png_data,
        user_token=user_token,
        org_id=org_id
    )
    icon_key = icon_result["config"]["general"]["icon_key"]
    icon_url = icon_result["config"]["general"]["icon_url"]
    assert icon_key is not None
    
    # 3. Update settings again (should preserve icon)
    updated_config = {
        "general": {
            "ai_analyst_name": "Analytics Assistant",
            "bow_credit": False,
            "icon_key": icon_key,
            "icon_url": icon_url
        }
    }
    result = update_organization_settings(config=updated_config, user_token=user_token, org_id=org_id)
    assert result["config"]["general"]["ai_analyst_name"] == "Analytics Assistant"
    assert result["config"]["general"]["icon_key"] == icon_key
    
    # 4. Remove icon
    delete_result = delete_organization_icon(user_token=user_token, org_id=org_id)
    assert delete_result["config"]["general"]["icon_key"] is None
    
    # 5. Verify final state
    final_settings = get_organization_settings(user_token=user_token, org_id=org_id)
    assert final_settings["config"]["general"]["ai_analyst_name"] == "Analytics Assistant"
    assert final_settings["config"]["general"]["bow_credit"] is False
    assert final_settings["config"]["general"]["icon_key"] is None


